"""Train with optional Global Distance, Local Distance, Identification Loss."""
from __future__ import print_function

import sys
sys.path.insert(0, '.')

import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.nn.parallel import DataParallel

import time
import os.path as osp
from tensorboardX import SummaryWriter
import numpy as np
import argparse
from random import randint
import random

import cv2
import os
from scipy.cluster.vq import *
from sklearn.preprocessing import StandardScaler
from sklearn.externals import joblib

from plus_vcfl.dataset import create_dataset
from plus_vcfl.model.Model import Model
from plus_vcfl.model.TripletLoss import TripletLoss
from plus_vcfl.model.loss import global_loss
from plus_vcfl.model.loss import local_loss
from plus_vcfl.model.loss import normalize

from plus_vcfl.utils.utils import time_str
from plus_vcfl.utils.utils import str2bool
from plus_vcfl.utils.utils import tight_float_str as tfs
from plus_vcfl.utils.utils import may_set_mode
from plus_vcfl.utils.utils import load_state_dict
from plus_vcfl.utils.utils import load_ckpt
from plus_vcfl.utils.utils import save_ckpt
from plus_vcfl.utils.utils import set_devices
from plus_vcfl.utils.utils import AverageMeter
from plus_vcfl.utils.utils import to_scalar
from plus_vcfl.utils.utils import ReDirectSTD
from plus_vcfl.utils.utils import set_seed
from plus_vcfl.utils.utils import adjust_lr_exp
from plus_vcfl.utils.utils import adjust_lr_staircase


class Config(object):
  def __init__(self):

    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--sys_device_ids', type=eval, default=(0,))
    parser.add_argument('-r', '--run', type=int, default=1)
    parser.add_argument('--set_seed', type=str2bool, default=False)
    parser.add_argument('--dataset', type=str, default='market1501',
                        choices=['market1501', 'cuhk03', 'duke', 'combined'])
    parser.add_argument('--trainset_part', type=str, default='trainval',
                        choices=['trainval', 'train'])

    # Only for training set.
    parser.add_argument('--resize_h_w', type=eval, default=(256, 128))
    parser.add_argument('--crop_prob', type=float, default=0)
    parser.add_argument('--crop_ratio', type=float, default=1)
    parser.add_argument('--ids_per_batch', type=int, default=32)
    parser.add_argument('--ims_per_id', type=int, default=4)

    parser.add_argument('--log_to_file', type=str2bool, default=True)
    parser.add_argument('--normalize_feature', type=str2bool, default=True)
    parser.add_argument('--local_dist_own_hard_sample',
                        type=str2bool, default=False)
    parser.add_argument('-gm', '--global_margin', type=float, default=0.3)
    parser.add_argument('-lm', '--local_margin', type=float, default=0.3)
    parser.add_argument('-glw', '--g_loss_weight', type=float, default=1.)
    parser.add_argument('-llw', '--l_loss_weight', type=float, default=0.)
    parser.add_argument('-idlw', '--id_loss_weight', type=float, default=0.)
    parser.add_argument('-slw', '--sift_loss_weight', type=float, default=0.)
    parser.add_argument('-clw', '--c_loss_weight', type=float, default=0.)    
    parser.add_argument('-vlw', '--view_loss_weight', type=float, default=0.)

    parser.add_argument('--only_test', type=str2bool, default=False)
    parser.add_argument('--resume', type=str2bool, default=False)
    parser.add_argument('--exp_dir', type=str, default='')
    parser.add_argument('--model_weight_file', type=str, default='')

    parser.add_argument('--base_lr', type=float, default=2e-4)
    parser.add_argument('--lr_decay_type', type=str, default='exp',
                        choices=['exp', 'staircase'])
    parser.add_argument('--exp_decay_at_epoch', type=int, default=76)
    parser.add_argument('--staircase_decay_at_epochs',
                        type=eval, default=(101, 201,))
    parser.add_argument('--staircase_decay_multiply_factor',
                        type=float, default=0.1)
    parser.add_argument('--total_epochs', type=int, default=150)

    args = parser.parse_known_args()[0]

    # gpu ids
    self.sys_device_ids = args.sys_device_ids

    if args.set_seed:
      self.seed = 1
    else:
      self.seed = None

    # The experiments can be run for several times and performances be averaged.
    # `run` starts from `1`, not `0`.
    self.run = args.run

    ###########
    # Dataset #
    ###########

    # If you want to exactly reproduce the result in training, you have to set
    # num of threads to 1.
    if self.seed is not None:
      self.prefetch_threads = 1
    else:
      self.prefetch_threads = 2

    self.dataset = args.dataset
    self.trainset_part = args.trainset_part

    # Image Processing

    # Just for training set
    self.crop_prob = args.crop_prob
    self.crop_ratio = args.crop_ratio
    self.resize_h_w = args.resize_h_w

    # Whether to scale by 1/255
    self.scale_im = True
    self.im_mean = [0.486, 0.459, 0.408]
    self.im_std = [0.229, 0.224, 0.225]

    self.ids_per_batch = args.ids_per_batch
    self.ims_per_id = args.ims_per_id
    self.train_final_batch = False
    self.train_mirror_type = ['random', 'always', None][0]
    self.train_shuffle = True

    self.test_batch_size = 32
    self.test_final_batch = True
    self.test_mirror_type = ['random', 'always', None][2]
    self.test_shuffle = False

    dataset_kwargs = dict(
      name=self.dataset,
      resize_h_w=self.resize_h_w,
      scale=self.scale_im,
      im_mean=self.im_mean,
      im_std=self.im_std,
      batch_dims='NCHW',
      num_prefetch_threads=self.prefetch_threads)

    prng = np.random
    if self.seed is not None:
      prng = np.random.RandomState(self.seed)
    self.train_set_kwargs = dict(
      part=self.trainset_part,
      ids_per_batch=self.ids_per_batch,
      ims_per_id=self.ims_per_id,
      final_batch=self.train_final_batch,
      shuffle=self.train_shuffle,
      crop_prob=self.crop_prob,
      crop_ratio=self.crop_ratio,
      mirror_type=self.train_mirror_type,
      prng=prng)
    self.train_set_kwargs.update(dataset_kwargs)

    prng = np.random
    if self.seed is not None:
      prng = np.random.RandomState(self.seed)
    self.test_set_kwargs = dict(
      part='test',
      batch_size=self.test_batch_size,
      final_batch=self.test_final_batch,
      shuffle=self.test_shuffle,
      mirror_type=self.test_mirror_type,
      prng=prng)
    self.test_set_kwargs.update(dataset_kwargs)

    ###############
    # ReID Model  #
    ###############

    self.local_dist_own_hard_sample = args.local_dist_own_hard_sample

    self.normalize_feature = args.normalize_feature

    self.local_conv_out_channels = 128
    self.global_margin = args.global_margin
    self.local_margin = args.local_margin

    # Identification Loss weight
    self.id_loss_weight = args.id_loss_weight

    self.sift_loss_weight = args.sift_loss_weight
    self.c_loss_weight = args.c_loss_weight 
    self.view_loss_weight = args.view_loss_weight

    # global loss weight
    self.g_loss_weight = args.g_loss_weight
    # local loss weight
    self.l_loss_weight = args.l_loss_weight

    #############
    # Training  #
    #############

    self.weight_decay = 0.0005

    # Initial learning rate
    self.base_lr = args.base_lr
    self.lr_decay_type = args.lr_decay_type
    self.exp_decay_at_epoch = args.exp_decay_at_epoch
    self.staircase_decay_at_epochs = args.staircase_decay_at_epochs
    self.staircase_decay_multiply_factor = args.staircase_decay_multiply_factor
    # Number of epochs to train
    self.total_epochs = args.total_epochs

    # How often (in batches) to log. If only need to log the average
    # information for each epoch, set this to a large value, e.g. 1e10.
    self.log_steps = 1e10

    # Only test and without training.
    self.only_test = args.only_test

    self.resume = args.resume

    #######
    # Log #
    #######

    # If True,
    # 1) stdout and stderr will be redirected to file,
    # 2) training loss etc will be written to tensorboard,
    # 3) checkpoint will be saved
    self.log_to_file = args.log_to_file

    # The root dir of logs.
    if args.exp_dir == '':
      self.exp_dir = osp.join(
        'exp/train_whole',
        '{}'.format(self.dataset),
        #
        ('nf_' if self.normalize_feature else 'not_nf_') +
        ('ohs_' if self.local_dist_own_hard_sample else 'not_ohs_') +
        'gm_{}_'.format(tfs(self.global_margin)) +
        'lm_{}_'.format(tfs(self.local_margin)) +
        'glw_{}_'.format(tfs(self.g_loss_weight)) +
        'llw_{}_'.format(tfs(self.l_loss_weight)) +
        'idlw_{}_'.format(tfs(self.id_loss_weight)) +
        'clw_{}_'.format(tfs(self.c_loss_weight)) +
        'slw_{}_'.format(tfs(self.sift_loss_weight)) +
        'vlw_{}_'.format(tfs(self.view_loss_weight)) +                        
        'lr_{}_'.format(tfs(self.base_lr)) +
        '{}_'.format(self.lr_decay_type) +
        ('decay_at_{}_'.format(self.exp_decay_at_epoch)
         if self.lr_decay_type == 'exp'
         else 'decay_at_{}_factor_{}_'.format(
          '_'.join([str(e) for e in args.staircase_decay_at_epochs]),
          tfs(self.staircase_decay_multiply_factor))) +
        'total_{}'.format(self.total_epochs),
        #
        'run{}'.format(self.run),
      )
    else:
      self.exp_dir = args.exp_dir

    self.stdout_file = osp.join(
      self.exp_dir, 'stdout_{}.txt'.format(time_str()))
    self.stderr_file = osp.join(
      self.exp_dir, 'stderr_{}.txt'.format(time_str()))

    # Saving model weights and optimizer states, for resuming.
    self.ckpt_file = osp.join(self.exp_dir, 'ckpt.pth')
    # Just for loading a pretrained model; no optimizer states is needed.
    self.model_weight_file = args.model_weight_file

class ExtractFeature(object):
  """A function to be called in the val/test set, to extract features.
  Args:
    TVT: A callable to transfer images to specific device.
  """

  def __init__(self, model, TVT):
    self.model = model
    self.TVT = TVT

  def __call__(self, ims):
    old_train_eval_model = self.model.training
    # Set eval mode.
    # Force all BN layers to use global mean and variance, also disable
    # dropout.
    self.model.eval()
    ims = Variable(self.TVT(torch.from_numpy(ims).float()))
    feat, global_feat, local_feat = self.model(ims)[:3]
    feat = feat.data.cpu().numpy()
    global_feat = global_feat.data.cpu().numpy()
    local_feat = local_feat.data.cpu().numpy()
    # Restore the model to its old train/eval mode.
    self.model.train(old_train_eval_model)
    return feat, global_feat, local_feat

class SoftmaxEntropyLoss(object):
    def __init__(self):
        self.nx = None
        self.ny = None
        self.loss = None        

    def __call__(self, nx, ny):
        self.nx = nx
        self.ny = ny
        self.loss = torch.mean(-torch.sum(ny * torch.log(F.softmax(nx, dim=1)), dim=1))
        return self.loss

class CenterLoss(nn.Module):
  """Center loss.
  
  Reference:
  Wen et al. A Discriminative Feature Learning Approach for Deep Face Recognition. ECCV 2016.
  
  Args:
      num_classes (int): number of classes.
      feat_dim (int): feature dimension.
  """
  def __init__(self, num_classes=10, feat_dim=2, use_gpu=True):
    super(CenterLoss, self).__init__()
    self.num_classes = num_classes
    self.feat_dim = feat_dim
    self.use_gpu = use_gpu

    if self.use_gpu:
        self.centers = nn.Parameter(torch.randn(self.num_classes, self.feat_dim).cuda())
    else:
        self.centers = nn.Parameter(torch.randn(self.num_classes, self.feat_dim))

  def forward(self, x, labels):
    """
    Args:
        x: feature matrix with shape (batch_size, feat_dim).
        labels: ground truth labels with shape (batch_size).
    """
    batch_size = x.size(0)
    distmat = torch.pow(x, 2).sum(dim=1, keepdim=True).expand(batch_size, self.num_classes) + \
              torch.pow(self.centers, 2).sum(dim=1, keepdim=True).expand(self.num_classes, batch_size).t()
    distmat.addmm_(1, -2, x, self.centers.t())

    classes = torch.arange(self.num_classes).long()
    if self.use_gpu: classes = classes.cuda()
    labels = labels.unsqueeze(1).expand(batch_size, self.num_classes)
    mask = labels.eq(classes.expand(batch_size, self.num_classes))

    dist = distmat * mask.float()
    loss = dist.clamp(min=1e-12, max=1e+12).sum() / batch_size

    return loss

class ExtractSift(object):
  """A function to be called in the val/test set, to extract Sift.
  Args:
    TVT: A callable to transfer images to specific device.
  """
  def __init__(self):
    self.ims = None
    self.des = None
    self.batch_size = None

  def __call__(self, ims):
    des_list = []
    self.ims = ims.cpu() 
    im = (self.ims.permute(0,2,3,1)).numpy()
    self.batch_size = im.shape[0]   
    sift = cv2.xfeatures2d.SIFT_create()
    for i in range(self.batch_size):
      gray= cv2.cvtColor(np.uint8(im[i,:,:,:]),cv2.COLOR_BGR2GRAY)
      kpts, des = sift.detectAndCompute(gray, None)
      if des is None:
        try:
          des = des_list[0]
        except:
          gray= cv2.cvtColor(np.uint8(im[i+1,:,:,:]),cv2.COLOR_BGR2GRAY)
          kpts, des = sift.detectAndCompute(gray, None)
      des_list.append(des)        
    descriptors = np.vstack(des_list)
  #   #Perform k-means clustering
    k = 2048
    voc,variance = kmeans(descriptors, k , 1)
    im_features = np.zeros((self.batch_size, k), "float32")
    for i in range(self.batch_size):
      words, distance = vq(des_list[i][:], voc)
      #print 'len %d' %(len(des_list[i][:]))
      for w in words:
          im_features[i][w]+=1
    # Perform Tf-idf vectorization
    nbr_occurences = np.sum((im_features>0)*1,axis = 0)
    idf = np.array(np.log((1.0*self.batch_size+1)/(1.0*nbr_occurences + 1)), 'float32')
    # Scaling the words
    stdSlr = StandardScaler().fit(im_features)
    im_features = stdSlr.transform(im_features)
    im_features = np.reshape(im_features,(self.batch_size,k))
    return im_features

def main():
  cfg = Config()

  # Redirect logs to both console and file.
  if cfg.log_to_file:
    ReDirectSTD(cfg.stdout_file, 'stdout', False)
    ReDirectSTD(cfg.stderr_file, 'stderr', False)

  # Lazily create SummaryWriter
  writer = None

  TVT, TMO = set_devices(cfg.sys_device_ids)

  if cfg.seed is not None:
    set_seed(cfg.seed)

  # Dump the configurations to log.
  import pprint
  print('-' * 60)
  print('cfg.__dict__')
  pprint.pprint(cfg.__dict__)
  print('-' * 60)

  ###########
  # Dataset #
  ###########

  train_set = create_dataset(**cfg.train_set_kwargs)

  test_sets = []
  test_set_names = []
  if cfg.dataset == 'combined':
    for name in ['market1501', 'cuhk03', 'duke']:
      cfg.test_set_kwargs['name'] = name
      test_sets.append(create_dataset(**cfg.test_set_kwargs))
      test_set_names.append(name)
  else:
    test_sets.append(create_dataset(**cfg.test_set_kwargs))
    test_set_names.append(cfg.dataset)

  ###########
  # Models  #
  ###########
  if cfg.dataset == 'market1501':
    cams = 6
  elif cfg.dataset == 'cuhk03':
    cams = 2
  else:
    cams = 8

  ids = len(train_set.ids2labels)

  model = Model(local_conv_out_channels=cfg.local_conv_out_channels,
                num_classes=len(train_set.ids2labels), cam_classes= cams)
  # Model wrapper
  model_w = DataParallel(model)

  #############################
  # Criteria and Optimizers   #
  #############################

  view_criterion = SoftmaxEntropyLoss()
  id_criterion = nn.CrossEntropyLoss()
  id_criterion1 = SoftmaxEntropyLoss()
  g_tri_loss = TripletLoss(margin=cfg.global_margin)
  l_tri_loss = TripletLoss(margin=cfg.local_margin)
  center_loss = CenterLoss(num_classes=len(train_set.ids2labels), feat_dim=2048, use_gpu=True)

  optimizer = optim.Adam(model.parameters(),
                         lr=cfg.base_lr,
                         weight_decay=cfg.weight_decay)
  optimizer_centloss = torch.optim.SGD(center_loss.parameters(), lr=0.001)

  # Bind them together just to save some codes in the following usage.
  modules_optims = [model, optimizer]

  ################################
  # May Resume Models and Optims #
  ################################

  if cfg.resume:
    resume_ep, scores = load_ckpt(modules_optims, cfg.ckpt_file)

  # May Transfer Models and Optims to Specified Device. Transferring optimizer
  # is to cope with the case when you load the checkpoint to a new device.
  TMO(modules_optims)

  ########
  # Test #
  ########

  def test(load_model_weight=False):
    if load_model_weight:
      if cfg.model_weight_file != '':
        map_location = (lambda storage, loc: storage)
        sd = torch.load(cfg.model_weight_file, map_location=map_location)
        load_state_dict(model, sd)
        print('Loaded model weights from {}'.format(cfg.model_weight_file))
      else:
        load_ckpt(modules_optims, cfg.ckpt_file)

    use_local_distance = (cfg.l_loss_weight > 0) \
                         and cfg.local_dist_own_hard_sample

    for test_set, name in zip(test_sets, test_set_names):
      test_set.set_feat_func(ExtractFeature(model_w, TVT))
      print('\n=========> Test on dataset: {} <=========\n'.format(name))
      test_set.eval(
        normalize_feat=cfg.normalize_feature,
        use_local_distance=use_local_distance)

  if cfg.only_test:
    test(load_model_weight=True)
    return

  ############
  # Training #
  ############

  start_ep = resume_ep if cfg.resume else 0
  for ep in range(start_ep, cfg.total_epochs):

    # Adjust Learning Rate
    if cfg.lr_decay_type == 'exp':
      adjust_lr_exp(
        optimizer,
        cfg.base_lr,
        ep + 1,
        cfg.total_epochs,
        cfg.exp_decay_at_epoch)
    if cfg.lr_decay_type == 'exp':
      adjust_lr_exp(
        optimizer_centloss,
        cfg.base_lr,
        ep + 1,
        cfg.total_epochs,
        cfg.exp_decay_at_epoch)  
    else:
      adjust_lr_staircase(
        optimizer,
        cfg.base_lr,
        ep + 1,
        cfg.staircase_decay_at_epochs,
        cfg.staircase_decay_multiply_factor)
      adjust_lr_staircase(
        optimizer_centloss,
        cfg.base_lr,
        ep + 1,
        cfg.staircase_decay_at_epochs,
        cfg.staircase_decay_multiply_factor)

    may_set_mode(modules_optims, 'train')

    g_prec_meter = AverageMeter()
    g_m_meter = AverageMeter()
    g_dist_ap_meter = AverageMeter()
    g_dist_an_meter = AverageMeter()
    g_loss_meter = AverageMeter()

    l_prec_meter = AverageMeter()
    l_m_meter = AverageMeter()
    l_dist_ap_meter = AverageMeter()
    l_dist_an_meter = AverageMeter()
    l_loss_meter = AverageMeter()

    id_loss_meter = AverageMeter()

    sift_loss_meter = AverageMeter()
    c_loss_meter = AverageMeter()

    view_loss_meter = AverageMeter()
        
    loss_meter = AverageMeter()

    ep_st = time.time()
    step = 0
    epoch_done = False
    while not epoch_done:

      step += 1
      step_st = time.time()

      ims, im_names, labels, cam_labels, mirrored, epoch_done = train_set.next_batch()

      ims_var = Variable(TVT(torch.from_numpy(ims).float()))
      labels_t = TVT(torch.from_numpy(labels).long())
      labels_var1 = Variable(labels_t)
###########################################id labels########################################
      m = torch.LongTensor(labels)
      batchsize = cfg.ids_per_batch * cfg.ims_per_id
      n = m.view(batchsize,1)   #96

      id_onehot = torch.FloatTensor(batchsize, ids)
      id_onehot.zero_()
      id_onehot.scatter_(1, n, 1)

      id_onehot = id_onehot*0.8    #0.8
      id_po = 0.2 / (ids-1)
      id_piil = torch.zeros(batchsize,ids)
      id_pik = id_piil +  id_po
      labels_var = torch.where(id_onehot>id_pik,id_onehot,id_pik).cuda()

#########################################cam labels#####################################
      if cfg.dataset == 'cuhk03':
        cam_labels_t = TVT(torch.from_numpy(cam_labels).long())
      else:
        cam_labels = cam_labels - 1
        cam_labels_t = TVT(torch.from_numpy(cam_labels).long())

      b = torch.LongTensor(cam_labels)
      batchsize = cfg.ids_per_batch * cfg.ims_per_id
      c = b.view(batchsize,1)   #96

      cam_onehot = torch.FloatTensor(batchsize, cams)
      cam_onehot.zero_()
      cam_onehot.scatter_(1, c, 1)

      cam_onehot = cam_onehot*0.8    #0.8
      cam_po = 0.2 / (cams-1)
      cam_piil = torch.zeros(batchsize,cams)
      cam_pik = cam_piil +  cam_po
      cam_labels_var = torch.where(cam_onehot>cam_pik,cam_onehot,cam_pik).cuda()
      #cam_labels_var = Variable(cam_labels_t)

      feat, global_feat, local_feat, logits, view_logits = model_w(ims_var)
      sift_func = ExtractSift()
      sift = torch.from_numpy(sift_func(ims_var)).cuda()

      g_loss, p_inds, n_inds, g_dist_ap, g_dist_an, g_dist_mat = global_loss(
        g_tri_loss, global_feat, labels_t,
        normalize_feature=cfg.normalize_feature)

      if cfg.l_loss_weight == 0:
        l_loss = 0
      elif cfg.local_dist_own_hard_sample:
        # Let local distance find its own hard samples.
        l_loss, l_dist_ap, l_dist_an, _ = local_loss(
          l_tri_loss, local_feat, None, None, labels_t,
          normalize_feature=cfg.normalize_feature)
      else:
        l_loss, l_dist_ap, l_dist_an = local_loss(
          l_tri_loss, local_feat, p_inds, n_inds, labels_t,
          normalize_feature=cfg.normalize_feature)

      id_loss = 0
      if cfg.id_loss_weight > 0:
        id_loss = id_criterion1(logits, labels_var)

      sift_loss = 0
      if cfg.sift_loss_weight > 0:
        #sift_loss = torch.norm(normalize(global_feat, axis=-1)-normalize(sift, axis=-1))
        sift_loss = torch.norm(F.softmax(global_feat,dim=1)-F.softmax(sift,dim=1))

      c_loss = 0
      if cfg.c_loss_weight > 0:
        c_loss = center_loss(normalize(global_feat, axis=-1), labels_var1)            

      view_loss = 0
      if cfg.view_loss_weight > 0:
        view_loss = view_criterion(view_logits, cam_labels_var)

      loss = g_loss * cfg.g_loss_weight \
             + l_loss * cfg.l_loss_weight \
             + id_loss * cfg.id_loss_weight \
             + sift_loss * cfg.sift_loss_weight \
             + c_loss * cfg.c_loss_weight \
             + view_loss * cfg.view_loss_weight        
      
      optimizer.zero_grad()
      optimizer_centloss.zero_grad()
      loss.backward()
      optimizer.step()
      if cfg.c_loss_weight > 0:
        for param in center_loss.parameters():
          param.grad.data *= (1 / cfg.c_loss_weight)
        optimizer_centloss.step()

      ############
      # Step Log #
      ############

      # precision
      g_prec = (g_dist_an > g_dist_ap).data.float().mean()
      # the proportion of triplets that satisfy margin
      g_m = (g_dist_an > g_dist_ap + cfg.global_margin).data.float().mean()
      g_d_ap = g_dist_ap.data.mean()
      g_d_an = g_dist_an.data.mean()

      g_prec_meter.update(g_prec)
      g_m_meter.update(g_m)
      g_dist_ap_meter.update(g_d_ap)
      g_dist_an_meter.update(g_d_an)
      g_loss_meter.update(to_scalar(g_loss))

      if cfg.l_loss_weight > 0:
        # precision
        l_prec = (l_dist_an > l_dist_ap).data.float().mean()
        # the proportion of triplets that satisfy margin
        l_m = (l_dist_an > l_dist_ap + cfg.local_margin).data.float().mean()
        l_d_ap = l_dist_ap.data.mean()
        l_d_an = l_dist_an.data.mean()

        l_prec_meter.update(l_prec)
        l_m_meter.update(l_m)
        l_dist_ap_meter.update(l_d_ap)
        l_dist_an_meter.update(l_d_an)
        l_loss_meter.update(to_scalar(l_loss))

      if cfg.id_loss_weight > 0:
        id_loss_meter.update(to_scalar(id_loss))

      if cfg.sift_loss_weight > 0:
        sift_loss_meter.update(to_scalar(sift_loss))

      if cfg.c_loss_weight > 0:
        c_loss_meter.update(to_scalar(c_loss))  

      if cfg.view_loss_weight > 0:
        view_loss_meter.update(to_scalar(view_loss))        


      loss_meter.update(to_scalar(loss))

      if step % cfg.log_steps == 0:
        time_log = '\tStep {}/Ep {}, {:.2f}s'.format(
          step, ep + 1, time.time() - step_st, )

        if cfg.g_loss_weight > 0:
          g_log = (', gp {:.2%}, gm {:.2%}, '
                   'gd_ap {:.4f}, gd_an {:.4f}, '
                   'gL {:.4f}'.format(
            g_prec_meter.val, g_m_meter.val,
            g_dist_ap_meter.val, g_dist_an_meter.val,
            g_loss_meter.val, ))
        else:
          g_log = ''

        if cfg.l_loss_weight > 0:
          l_log = (', lp {:.2%}, lm {:.2%}, '
                   'ld_ap {:.4f}, ld_an {:.4f}, '
                   'lL {:.4f}'.format(
            l_prec_meter.val, l_m_meter.val,
            l_dist_ap_meter.val, l_dist_an_meter.val,
            l_loss_meter.val, ))
        else:
          l_log = ''

        if cfg.id_loss_weight > 0:
          id_log = (', idL {:.4f}'.format(id_loss_meter.val))
        else:
          id_log = ''

        if cfg.sift_loss_weight > 0:
          sift_log = (', sL {:.4f}'.format(sift_loss_meter.val))
        else:
          sift_log = ''

        if cfg.c_loss_weight > 0:
          c_log = (', cL {:.4f}'.format(c_loss_meter.val))
        else:
          c_log = ''  

        if cfg.view_loss_weight > 0:
          view_log = (', viewL {:.4f}'.format(view_loss_meter.val))
        else:
          view_log = ''         

        total_loss_log = ', loss {:.4f}'.format(loss_meter.val)

        log = time_log + \
              g_log + l_log + id_log + \
              sift_log + c_log + view_log + \
              total_loss_log
        print(log)

    #############
    # Epoch Log #
    #############

    time_log = 'Ep {}, {:.2f}s'.format(ep + 1, time.time() - ep_st, )

    if cfg.g_loss_weight > 0:
      g_log = (', gp {:.2%}, gm {:.2%}, '
               'gd_ap {:.4f}, gd_an {:.4f}, '
               'gL {:.4f}'.format(
        g_prec_meter.avg, g_m_meter.avg,
        g_dist_ap_meter.avg, g_dist_an_meter.avg,
        g_loss_meter.avg, ))
    else:
      g_log = ''

    if cfg.l_loss_weight > 0:
      l_log = (', lp {:.2%}, lm {:.2%}, '
               'ld_ap {:.4f}, ld_an {:.4f}, '
               'lL {:.4f}'.format(
        l_prec_meter.avg, l_m_meter.avg,
        l_dist_ap_meter.avg, l_dist_an_meter.avg,
        l_loss_meter.avg, ))
    else:
      l_log = ''

    if cfg.id_loss_weight > 0:
      id_log = (', idL {:.4f}'.format(id_loss_meter.avg))
    else:
      id_log = ''

    if cfg.sift_loss_weight > 0:
      sift_log = (', sL {:.4f}'.format(sift_loss_meter.avg))
    else:
      sift_log = '' 

    if cfg.c_loss_weight > 0:
      c_log = (', cL {:.4f}'.format(c_loss_meter.avg))
    else:
      c_log = ''    

    if cfg.view_loss_weight > 0:
      view_log = (', viewL {:.4f}'.format(view_loss_meter.avg))
    else:
      view_log = ''      

    total_loss_log = ', loss {:.4f}'.format(loss_meter.avg)

    log = time_log + \
          g_log + l_log + id_log + \
          sift_log + c_log + view_log + \
          total_loss_log
    print(log)

    # Log to TensorBoard

    if cfg.log_to_file:
      if writer is None:
        writer = SummaryWriter(log_dir=osp.join(cfg.exp_dir, 'tensorboard'))
      writer.add_scalars(
        'loss',
        dict(global_loss=g_loss_meter.avg,
             local_loss=l_loss_meter.avg,
             id_loss=id_loss_meter.avg,
             sift_loss=sift_loss_meter.avg,
             c_loss=c_loss_meter.avg, 
             view_loss=view_loss_meter.avg,                         
             loss=loss_meter.avg, ),
        ep)
      writer.add_scalars(
        'tri_precision',
        dict(global_precision=g_prec_meter.avg,
             local_precision=l_prec_meter.avg, ),
        ep)
      writer.add_scalars(
        'satisfy_margin',
        dict(global_satisfy_margin=g_m_meter.avg,
             local_satisfy_margin=l_m_meter.avg, ),
        ep)
      writer.add_scalars(
        'global_dist',
        dict(global_dist_ap=g_dist_ap_meter.avg,
             global_dist_an=g_dist_an_meter.avg, ),
        ep)
      writer.add_scalars(
        'local_dist',
        dict(local_dist_ap=l_dist_ap_meter.avg,
             local_dist_an=l_dist_an_meter.avg, ),
        ep)

    # save ckpt
    if cfg.log_to_file:
      save_ckpt(modules_optims, ep + 1, 0, cfg.ckpt_file)

  ########
  # Test #
  ########

  test(load_model_weight=False)


if __name__ == '__main__':
  main()
