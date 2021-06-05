'''
 *
 * SIDCo - Efficient Statistical-based Compression Technique for Distributed ML.
 *
 *  Author: Ahmed Mohamed Abdelmoniem Sayed, <ahmedcs982@gmail.com, github:ahmedcs>
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of CRAPL LICENCE avaliable at
 *    http://matt.might.net/articles/crapl/
 *    http://matt.might.net/articles/crapl/CRAPL-LICENSE.txt
 *
 * This program is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
 * See the CRAPL LICENSE for more details.
 *
 * Please READ carefully the attached README and LICENCE file with this software
 *
 '''


# -*- coding: utf-8 -*-
from __future__ import print_function
import os
import argparse
import time

import torch
import torchvision
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models
import torch.distributed as dist
import torch.utils.data.distributed
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.cuda as ct
import settings
import torch.backends.cudnn as cudnn
cudnn.benchmark = False
cudnn.deterministic = True
from settings import logger, formatter
import struct
import models
import logging
import utils
import math
#from tensorboardX import SummaryWriter
#from datasets import DatasetHDF5
from profiling import benchmark
#writer = SummaryWriter()

from logger import TensorboardLogger #, FileLogger

import ptb_reader
import models.lstm as lstmpy
from torch.autograd import Variable
import json

import wandb

if settings.USE_FP16:
    try:
        import apex
    except:
        apex = None
else:
    apex = None

torch.set_num_threads(1)

_support_datasets = ['imagenet', 'cifar10', 'an4', 'ptb', 'mnist']
_support_dnns = ['resnet50', 'googlenet', 'inceptionv4', 'inceptionv3', 'vgg16i', 'alexnet', \
                 'resnet20', 'resnet56', 'resnet110', 'vgg19', 'vgg16',  \
                 'lstman4', \
                 'lstm', \
                 'mnistnet', 'fcn5net', 'lenet', 'lr']

NUM_CPU_THREADS=6

#Tensorboard logger and Filelogger
class MnistNet(nn.Module):
    def __init__(self):
        super(MnistNet, self).__init__()
        self.conv1 = nn.Conv2d(1, 10, kernel_size=5)
        self.conv2 = nn.Conv2d(10, 20, 5)
        self.conv2_drop = nn.Dropout2d()
        self.fc1 = nn.Linear(320, 50)
        self.fc2 = nn.Linear(50, 10)
        self.name = 'mnistnet'

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 2))
        x = F.relu(F.max_pool2d(self.conv2_drop(self.conv2(x)), 2))
        x = x.view(-1, 320)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, training=self.training)
        x = self.fc2(x)
        return x


def get_available_gpu_device_ids(ngpus):
    return range(0, ngpus)

def create_net(num_classes, dnn='resnet20', **kwargs):
    ext = None
    if dnn in ['resnet20', 'resnet56', 'resnet110']:
        net = models.__dict__[dnn](num_classes=num_classes)
    elif dnn == 'resnet50':
        #net = models.__dict__['resnet50'](num_classes=num_classes)
        net = torchvision.models.resnet50(num_classes=num_classes)
    elif dnn == 'inceptionv4':
        net = models.inceptionv4(num_classes=num_classes)
    elif dnn == 'inceptionv3':
        net = torchvision.models.inception_v3(num_classes=num_classes)
    elif dnn == 'vgg16i': # vgg16 for imagenet
        net = torchvision.models.vgg16(num_classes=num_classes)
    elif dnn == 'vgg19': # vgg19 for imagenet
        net = torchvision.models.vgg19(num_classes=num_classes)
    elif dnn == 'googlenet':
        net = models.googlenet()
    elif dnn == 'mnistnet':
        net = MnistNet()
    elif dnn == 'fcn5net':
        net = models.FCN5Net()
    elif dnn == 'lenet':
        net = models.LeNet()
    elif dnn == 'lr':
        net = models.LinearRegression()
    elif dnn == 'vgg16':
        net = models.VGG(dnn.upper())
    elif dnn == 'alexnet':
        net = torchvision.models.alexnet()
    elif dnn == 'lstman4':
        net, ext = models.LSTMAN4(datapath=kwargs['datapath'])
    elif dnn == 'lstm':
        net = lstmpy.lstm(vocab_size=kwargs['vocab_size'], batch_size=kwargs['batch_size'])

    else:
        errstr = 'Unsupport neural network %s' % dnn
        logger.error(errstr)
        raise errstr 
    return net, ext


class DLTrainer:

    def __init__(self, rank, size, master='gpu10', dist=True, ngpus=1, batch_size=32, 
        is_weak_scaling=True, data_dir='./data', dataset='cifar10', dnn='resnet20', 
        lr=0.04, nworkers=1, prefix=None, sparsity=0.95, pretrain=None, num_steps=35, tb_writer=None, amp_handle=None,
                 tb=None, optimizer_str='nesterov'):

        #Ahmed - Add tensorboard (WANDB) to class
        self.tb = tb
        self._optimizer_str = optimizer_str

        self.iter_times = []

        self.size = size
        self.rank = rank
        self.pretrain = pretrain
        self.dataset = dataset
        self.prefix=prefix
        self.num_steps = num_steps
        self.ngpus = ngpus
        self.writer = tb_writer
        self.amp_handle = amp_handle
        if self.ngpus > 0:
            self.batch_size = batch_size * self.ngpus if is_weak_scaling else batch_size
        else:
            self.batch_size = batch_size
        self.num_batches_per_epoch = -1
        if self.dataset == 'cifar10' or self.dataset == 'mnist':
            self.num_classes = 10
        elif self.dataset == 'imagenet':
            self.num_classes = 1000
        elif self.dataset == 'an4':
            self.num_classes = 29 
        elif self.dataset == 'ptb':
            self.num_classes = 10
        self.nworkers = nworkers # just for easy comparison
        self.data_dir = data_dir
        if type(dnn) != str:
            self.net = dnn
            self.dnn = dnn.name
            self.ext = None # leave for further parameters
        else:
            self.dnn = dnn
            # TODO: Refact these codes!
            if self.dnn == 'lstm':
                if data_dir is not None:
                    self.data_prepare()
                self.net, self.ext = create_net(self.num_classes, self.dnn, vocab_size = self.vocab_size, batch_size=self.batch_size)
            elif self.dnn == 'lstman4':
                self.net, self.ext = create_net(self.num_classes, self.dnn, datapath=self.data_dir)
                if data_dir is not None:
                    self.data_prepare()
            else:
                if data_dir is not None:
                    self.data_prepare()
                self.net, self.ext = create_net(self.num_classes, self.dnn)

        self.lr = lr
        self.base_lr = self.lr
        self.is_cuda = self.ngpus > 0

        if self.is_cuda:
            if self.ngpus > 1:
                devices = get_available_gpu_device_ids(ngpus)
                self.net = torch.nn.DataParallel(self.net, device_ids=devices).cuda()
            else:
                self.net.cuda()
        self.net.share_memory()
        self.accuracy = 0
        self.loss = 0.0
        self.train_iter = 0
        self.recved_counter = 0
        self.master = master
        self.average_iter = 0
        if self.dataset != 'an4':
            if self.is_cuda:
                self.criterion = nn.CrossEntropyLoss().cuda()
            else:
                self.criterion = nn.CrossEntropyLoss()
        else:
            from warpctc_pytorch import CTCLoss
            self.criterion = CTCLoss()
        weight_decay = 1e-4
        self.m = 0.9 # momentum
        nesterov = False
        if self.dataset == 'an4':
            #nesterov = True
            self.lstman4_lr_epoch_tag = 0
            #weight_decay = 0.
        elif self.dataset == 'ptb':
            self.m = 0
            weight_decay = 0
        elif self.dataset == 'imagenet':
            #weight_decay = 5e-4
            self.m = 0.875
            weight_decay = 2*3.0517578125e-05

        decay = []
        no_decay = []
        for name, param in self.net.named_parameters():
            if not param.requires_grad:
                continue
            if len(param.shape) == 1 or 'bn' in name or 'bias' in name:
                no_decay.append(param)
            else:
                decay.append(param)
        parameters = [{'params': no_decay, 'weight_decay': 0.},
                    {'params': decay, 'weight_decay': weight_decay}]

        if optimizer_str == 'nesterov':
            self.optimizer = optim.SGD(parameters, lr=self.lr, weight_decay=weight_decay, momentum=self.m, nesterov=nesterov)
        elif optimizer_str == 'momentum':
            self.optimizer = optim.SGD(parameters, lr=self.lr, weight_decay=weight_decay, momentum=self.m)
        elif optimizer_str == 'sgd':
            self.optimizer = optim.SGD(parameters, lr=self.lr, weight_decay=weight_decay)
        elif optimizer_str == 'eclearn':
            self.optimizer = optim.SGD(parameters, lr=1.0, weight_decay=weight_decay)

        self.train_epoch = 0

        if self.pretrain is not None and os.path.isfile(self.pretrain):
            self.load_model_from_file(self.pretrain)

        self.sparsities = []
        self.compression_ratios = []
        self.communication_sizes = []
        self.remainer = {}
        self.v = {} 
        self.sparsity = sparsity
        self.avg_loss_per_epoch = 0.0
        self.timer = 0.0
        self.forwardtime = 0.0
        self.backwardtime = 0.0
        self.iotime = 0.0
        self.epochs_info = []
        self.distributions = {}
        self.gpu_caches = {}
        self.delays = []
        self.num_of_updates_during_comm = 0 
        self.train_acc_top1 = []
        self.train_acc_top5 = []
        if apex is not None:
            self.init_fp16()
        logger.info('num_batches_per_epoch: %d'% self.num_batches_per_epoch)

    def init_fp16(self):
        model, optim = apex.amp.initialize(self.net, self.optimizer, opt_level='O2', loss_scale=128.0)
        self.net = model
        self.optimizer = optim

    def get_acc(self):
        return self.accuracy

    def get_loss(self):
        return self.loss

    def get_model_state(self):
        return self.net.state_dict()

    def get_data_shape(self):
        return self._input_shape, self._output_shape

    def get_train_epoch(self):
        return self.train_epoch

    def get_train_iter(self):
        return self.train_iter

    def set_train_epoch(self, epoch):
        self.train_epoch = epoch

    def set_train_iter(self, iteration):
        self.train_iter = iteration

    def load_model_from_file(self, filename):
        checkpoint = torch.load(filename)
        self.net.load_state_dict(checkpoint['state'])
        self.train_epoch = checkpoint['epoch']
        self.train_iter = checkpoint['iter']
        logger.info('Load pretrain model: %s, start from epoch %d and iter: %d', filename, self.train_epoch, self.train_iter)

    def get_num_of_training_samples(self):
        return len(self.trainset)

    def imagenet_prepare(self):
        # Data loading code
        traindir = os.path.join(self.data_dir, 'train')
        if os.path.exists(os.path.join(self.data_dir, 'validation')):
            testdir = os.path.join(self.data_dir, 'validation')
        else:
            testdir = os.path.join(self.data_dir, 'val')
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                         std=[0.229, 0.224, 0.225])

        train_dataset = datasets.ImageFolder(
            traindir,
            transforms.Compose([
                transforms.RandomResizedCrop(224),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                normalize,
            ]))

        image_size = 224
        self._input_shape = (self.batch_size, 3, image_size, image_size)
        self._output_shape = (self.batch_size, 1000)

        self.trainset = train_dataset

        train_sampler = None
        shuffle = True
        if self.nworkers > 1: 
            train_sampler = torch.utils.data.distributed.DistributedSampler(self.trainset, num_replicas=self.nworkers, rank=self.rank)
            train_sampler.set_epoch(0)
            shuffle = False
        self.train_sampler = train_sampler

        self.trainloader = torch.utils.data.DataLoader(
            train_dataset,
            batch_size=self.batch_size, shuffle=shuffle,
            num_workers=NUM_CPU_THREADS, pin_memory=True, sampler=train_sampler)

        testset = torchvision.datasets.ImageFolder(testdir, transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            normalize,
            ]))

        self.testset = testset
        batch_size = self.batch_size * 4
        if self.dnn == 'vgg19':
            batch_size = self.batch_size * 2
        self.testloader = torch.utils.data.DataLoader(
            testset,
            batch_size=batch_size, shuffle=False,
            num_workers=NUM_CPU_THREADS, pin_memory=True)

    def cifar10_prepare(self):
        image_size = 32
        self._input_shape = (self.batch_size, 3, image_size, image_size)
        self._output_shape = (self.batch_size, 10)
        normalize = transforms.Normalize(mean=[0.491, 0.482, 0.447], std=[0.247, 0.243, 0.262])
        train_transform = transforms.Compose([
            transforms.RandomCrop(image_size, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize,
            ])
        test_transform = transforms.Compose([
                transforms.ToTensor(),
                normalize,
                ])
        trainset = torchvision.datasets.CIFAR10(root=self.data_dir, train=True,
                                                download=True, transform=train_transform)
        testset = torchvision.datasets.CIFAR10(root=self.data_dir, train=False,
                                               download=True, transform=test_transform)
        self.trainset = trainset
        self.testset = testset

        train_sampler = None
        shuffle = True
        if self.nworkers > 1: 
            train_sampler = torch.utils.data.distributed.DistributedSampler(
                self.trainset, num_replicas=self.nworkers, rank=self.rank)
            train_sampler.set_epoch(0)
            shuffle = False
        self.train_sampler = train_sampler
        self.trainloader = torch.utils.data.DataLoader(trainset, batch_size=self.batch_size,
                                                  shuffle=shuffle, num_workers=NUM_CPU_THREADS, sampler=train_sampler)
        self.testloader = torch.utils.data.DataLoader(testset, batch_size=self.batch_size * 4,
                                                 shuffle=False, num_workers=NUM_CPU_THREADS)
        self.classes = ('plane', 'car', 'bird', 'cat',
               'deer', 'dog', 'frog', 'horse', 'ship', 'truck')

    def mnist_prepare(self):
        trans = []
        if self.dnn == 'lenet':
            image_size = 32
            trans.append(transforms.Resize(32))
        else:
            image_size = 28
        trans.extend([
                        transforms.ToTensor(),
                        transforms.Normalize((0.1307,), (0.3081,))
                        ])
        self._input_shape = (self.batch_size, 1, image_size, image_size)
        self._output_shape = (self.batch_size, 10)

        trainset = torchvision.datasets.MNIST(self.data_dir, train=True, download=True,
                    transform=transforms.Compose(trans))
        self.trainset = trainset
        testset = torchvision.datasets.MNIST(self.data_dir, train=False, transform=transforms.Compose(trans))
        self.testset = testset
        train_sampler = None
        shuffle = True
        if self.nworkers > 1: 
            train_sampler = torch.utils.data.distributed.DistributedSampler(
                self.trainset, num_replicas=self.nworkers, rank=self.rank)
            train_sampler.set_epoch(0)
            shuffle = False
        self.train_sampler = train_sampler

        self.trainloader = torch.utils.data.DataLoader(trainset,
                batch_size=self.batch_size, shuffle=shuffle, num_workers=NUM_CPU_THREADS, sampler=train_sampler)
        self.testloader = torch.utils.data.DataLoader(
                testset,
                batch_size=self.batch_size * 4, shuffle=False, num_workers=NUM_CPU_THREADS)
    def ptb_prepare(self):
        # Data loading code
        # =====================================
        # num_workers=NUM_CPU_THREADS num_workers=1
        # batch_size=self.batch_size
        # num_steps = 35
        # hidden_size = 1500
        # =================================
        raw_data = ptb_reader.ptb_raw_data(data_path=self.data_dir)
        train_data, valid_data, test_data, word_to_id, id_2_word = raw_data
        self.vocab_size = len(word_to_id)


        self._input_shape = (self.batch_size, self.num_steps)
        self._output_shape = (self.batch_size, self.num_steps)

        epoch_size = ((len(train_data) // self.batch_size) - 1) // self.num_steps

        train_set = ptb_reader.TrainDataset(train_data, self.batch_size, self.num_steps)
        self.trainset = train_set
        train_sampler = None
        shuffle = True
        if self.nworkers > 1: 
            train_sampler = torch.utils.data.distributed.DistributedSampler(
                self.trainset, num_replicas=self.nworkers, rank=self.rank)
            train_sampler.set_epoch(0)
            shuffle = False
        self.train_sampler = train_sampler
        self.trainloader = torch.utils.data.DataLoader(
            train_set,
            batch_size=self.batch_size, shuffle=shuffle,
            num_workers=NUM_CPU_THREADS, pin_memory=True, sampler=train_sampler)


        test_set = ptb_reader.TestDataset(valid_data, self.batch_size, self.num_steps)
        self.testset = test_set
        self.testloader = torch.utils.data.DataLoader(
            test_set,
            batch_size=self.batch_size, shuffle=False,
            num_workers=NUM_CPU_THREADS, pin_memory=True)

    def an4_prepare(self):
        from audio_data.data_loader import AudioDataLoader, SpectrogramDataset, BucketingSampler, DistributedBucketingSampler
        from decoder import GreedyDecoder
        audio_conf = self.ext['audio_conf']
        labels = self.ext['labels']
        train_manifest = os.path.join(self.data_dir, 'an4_train_manifest.csv')
        val_manifest = os.path.join(self.data_dir, 'an4_val_manifest.csv')

        with open('labels.json') as label_file:
            labels = str(''.join(json.load(label_file)))
        trainset = SpectrogramDataset(audio_conf=audio_conf, manifest_filepath=train_manifest, labels=labels, normalize=True, spec_augment=True)
        self.trainset = trainset
        testset = SpectrogramDataset(audio_conf=audio_conf, manifest_filepath=val_manifest, labels=labels, normalize=True, spec_augment=False)
        self.testset = testset

        if self.nworkers > 1:
            train_sampler = DistributedBucketingSampler(self.trainset, batch_size=self.batch_size, num_replicas=self.nworkers, rank=self.rank)
        else:
            train_sampler = BucketingSampler(self.trainset, batch_size=self.batch_size)

        self.train_sampler = train_sampler
        trainloader = AudioDataLoader(self.trainset, num_workers=NUM_CPU_THREADS, batch_sampler=self.train_sampler)
        testloader = AudioDataLoader(self.testset, batch_size=self.batch_size, num_workers=NUM_CPU_THREADS)
        self.trainloader = trainloader
        self.testloader = testloader
        decoder = GreedyDecoder(labels)
        self.decoder = decoder


    def data_prepare(self):
        if self.dataset == 'imagenet':
            self.imagenet_prepare()
        elif self.dataset == 'cifar10':
            self.cifar10_prepare()
        elif self.dataset == 'mnist':
            self.mnist_prepare()
        elif self.dataset == 'an4':
            self.an4_prepare()
        elif self.dataset == 'ptb':
            self.ptb_prepare()
        else:
            errstr = 'Unsupport dataset: %s' % self.dataset
            logger.error(errstr)
            raise errstr
        self.data_iterator = iter(self.trainloader)
        self.num_batches_per_epoch = (self.get_num_of_training_samples()+self.batch_size*self.nworkers-1)//(self.batch_size*self.nworkers)
        #self.num_batches_per_epoch = self.get_num_of_training_samples()/(self.batch_size*self.nworkers)

    def update_optimizer(self, optimizer):
        self.optimizer = optimizer

    def update_nworker(self, nworkers, new_rank=-1):
        if new_rank >= 0:
            rank = new_rank
            self.nworkers = nworkers
        else:
            reduced_worker = self.nworkers - nworkers
            rank = self.rank
            if reduced_worker > 0 and self.rank >= reduced_worker:
                rank = self.rank - reduced_worker
        self.rank = rank
        if self.dnn != 'lstman4':
            train_sampler = torch.utils.data.distributed.DistributedSampler(self.trainset, num_replicas=nworkers, rank=rank)
            train_sampler.set_epoch(self.train_epoch)
            shuffle = False
            self.train_sampler = train_sampler
            self.trainloader = torch.utils.data.DataLoader(self.trainset, batch_size=self.batch_size,
                                                      shuffle=shuffle, num_workers=NUM_CPU_THREADS, sampler=train_sampler)
            self.testloader = torch.utils.data.DataLoader(self.testset, batch_size=self.batch_size * 4,
                                                     shuffle=False, num_workers=1)
        self.nworkers = nworkers
        self.num_batches_per_epoch = (self.get_num_of_training_samples()+self.batch_size*self.nworkers-1)//(self.batch_size*self.nworkers)

    def data_iter(self):
        try:
            d = self.data_iterator.next()
        except:
            self.data_iterator = iter(self.trainloader)
            d = self.data_iterator.next()
        if d[0].size()[0] != self.batch_size:
            return self.data_iter()
        return d

    def _adjust_learning_rate_lstman4(self, progress, optimizer):
        if self.lstman4_lr_epoch_tag != progress:
            self.lstman4_lr_epoch_tag = progress 
            for param_group in optimizer.param_groups:
                param_group['lr'] /= 1.01 
            self.lr = self.lr / 1.01

    def _adjust_learning_rate_lstmptb(self, progress, optimizer):
        first = 23+40
        second = 60
        third = 80
        if progress < first: 
            lr = self.base_lr
        elif progress < second: 
            lr = self.base_lr *0.1
        elif progress < third:
            lr = self.base_lr *0.01
        else:
            lr = self.base_lr *0.001
        self.lr = lr
        for param_group in optimizer.param_groups:
            param_group['lr'] = self.lr
        return self.lr 

    def _adjust_learning_rate_general(self, progress, optimizer):
        warmup = 5
        if settings.WARMUP and progress < warmup:
            warmup_total_iters = self.num_batches_per_epoch * warmup
            min_lr = self.base_lr / warmup_total_iters 
            lr_interval = (self.base_lr - min_lr) / warmup_total_iters
            self.lr = min_lr + lr_interval * self.train_iter
            for param_group in optimizer.param_groups:
                param_group['lr'] = self.lr
            return self.lr
        #Ahmed - change the adjustment epochs
        first = 80 #originally - 81
        second = first + 40 #originally -  first + 41
        third = second + 30 #originally -  second+33
        # Ahmed - Add CIFAR10
        # if self.dataset == 'cifar10':
        #     if self._optimizer_str == 'sgd':
        #         first = 70
        #         second = 90
        #         third = 120
        #     else:
        #         first = 40
        #         second = 60
        #         third = 100
        if self.dataset == 'imagenet':
            first = 30
            second = 60
            third = 80
        elif self.dataset == 'ptb':
            first = 24
            second = 60
            third = 80
        if progress < first: #40:  30 for ResNet-50, 40 for ResNet-20
            lr = self.base_lr
        elif progress < second: #80: 70 for ResNet-50, 80 for ResNet-20
            lr = self.base_lr * 0.1
        elif progress < third:
            lr = self.base_lr * 0.01
        else:
            lr = self.base_lr * 0.001
        self.lr = lr
        for param_group in optimizer.param_groups:
            param_group['lr'] = self.lr
        return self.lr 


    def adjust_learning_rate(self, progress, optimizer):
        if self.dnn == 'lstman4':
           return self._adjust_learning_rate_lstman4(self.train_iter//self.num_batches_per_epoch, optimizer)
        elif self.dnn == 'lstm':
            return self._adjust_learning_rate_lstmptb(progress, optimizer)
        return self._adjust_learning_rate_general(progress, optimizer)

    def finish(self):
        if self.writer is not None:
            self.writer.close()

    def cal_accuracy(self, output, target, topk=(1,)):
        """Computes the accuracy over the k top predictions for the specified values of k"""
        with torch.no_grad():
            maxk = max(topk)
            batch_size = target.size(0)
            _, pred = output.topk(maxk, 1, True, True)
            pred = pred.t()
            correct = pred.eq(target.view(1, -1).expand_as(pred))
            res = []
            for k in topk:
                correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
                res.append(correct_k.mul_(100.0 / batch_size))
            return res

    def train(self, num_of_iters=1, data=None, hidden=None):
        # Ahmed - Train Monitors
        timer = TimeMeter()
        #losses = AverageMeter()
        #top1 = AverageMeter()
        #top5 = AverageMeter()

        self.loss = 0.0
        # zero the parameter gradients
        #self.optimizer.zero_grad()
        for i in range(num_of_iters):
            s = time.time()
            self.adjust_learning_rate(self.train_epoch, self.optimizer)
            if self.train_iter % self.num_batches_per_epoch == 0 and self.train_iter > 0:
                self.train_epoch += 1
                logger.info('train iter: %d, num_batches_per_epoch: %d', self.train_iter, self.num_batches_per_epoch)
                logger.info('Epoch %d, avg train acc: %f, lr: %f, avg loss: %f' % (self.train_iter//self.num_batches_per_epoch, np.mean(self.train_acc_top1), self.lr, self.avg_loss_per_epoch/self.num_batches_per_epoch))

                if self.rank == 0 and self.writer is not None:
                    self.writer.add_scalar('cross_entropy', self.avg_loss_per_epoch/self.num_batches_per_epoch, self.train_epoch)
                    self.writer.add_scalar('top-1_acc', np.mean(self.train_acc_top1), self.train_epoch)
                #Ahmed - Test on every rank after each epoch for other datasets and after 5 Epochs for imagenet
                #if self.rank == 0:
                if self.dataset == 'imagenet':
                    if self.train_epoch % 5 == 0:
                        self.test(self.train_epoch)
                else:
                    self.test(self.train_epoch)

                self.sparsities = []
                self.compression_ratios = []
                self.communication_sizes = []
                self.train_acc_top1 = []
                self.train_acc_top5 = []
                self.epochs_info.append(self.avg_loss_per_epoch/self.num_batches_per_epoch)
                self.avg_loss_per_epoch = 0.0

                # Save checkpoint
                if self.train_iter > 0 and self.rank == 0:
                    state = {'iter': self.train_iter, 'epoch': self.train_epoch, 'state': self.get_model_state()}
                    if self.prefix:
                        relative_path = './weights/%s/%s-n%d-bs%d-lr%.4f' % (self.prefix, self.dnn, self.nworkers, self.batch_size, self.base_lr)
                    else:
                        relative_path = './weights/%s-n%d-bs%d-lr%.4f' % (self.dnn, self.nworkers, self.batch_size, self.base_lr)
                    utils.create_path(relative_path)
                    filename = '%s-rank%d-epoch%d.pth'%(self.dnn, self.rank, self.train_epoch)
                    fn = os.path.join(relative_path, filename)
                    if self.train_epoch % 2== 0:
                        self.save_checkpoint(state, fn)
                        self.remove_dict(state)
                if self.dnn != 'lstman4' and self.train_sampler and (self.nworkers > 1):
                    self.train_sampler.set_epoch(self.train_epoch)

            ss = time.time()

            if data is None:
                data = self.data_iter()

            if self.dataset == 'an4':
                inputs, labels_cpu, input_percentages, target_sizes = data
                input_sizes = input_percentages.mul_(int(inputs.size(3))).int()
            else:
                inputs, labels_cpu = data
            if self.is_cuda:
                if self.dnn == 'lstm' :
                    inputs = Variable(inputs.transpose(0, 1).contiguous()).cuda()
                    labels = Variable(labels_cpu.transpose(0, 1).contiguous()).cuda()
                else:
                    inputs, labels = inputs.cuda(non_blocking=True), labels_cpu.cuda(non_blocking=True)
            else:
                labels = labels_cpu
                
            self.iotime += (time.time() - ss)

            # Ahmed - start timer of the iteration - previous time is for data loading
            timer.batch_start()

            sforward = time.time()
            if self.dnn == 'lstman4':
                out, output_sizes = self.net(inputs, input_sizes)
                out = out.transpose(0, 1)  # TxNxH
                loss = self.criterion(out, labels_cpu, output_sizes, target_sizes)
                loss = loss / inputs.size(0)  # average the loss by minibatch
            elif self.dnn == 'lstm' :
                hidden = lstmpy.repackage_hidden(hidden)
                outputs, hidden = self.net(inputs, hidden)
                tt = torch.squeeze(labels.view(-1, self.net.batch_size * self.net.num_steps))
                loss = self.criterion(outputs.view(-1, self.net.vocab_size), tt)
            else:
                # forward + backward + optimize
                outputs = self.net(inputs)
                loss = self.criterion(outputs, labels)
            torch.cuda.synchronize()
            self.forwardtime += (time.time() - sforward)

            sbackward = time.time()
            if self.amp_handle is not None:
                with apex.amp.scale_loss(loss, self.optimizer) as scaled_loss:
                    scaled_loss.backward()
                    loss = scaled_loss
            else:
                loss.backward()
            loss_value = loss.item()
            torch.cuda.synchronize()
            self.backwardtime += (time.time() - sbackward)

            self.loss += loss_value
            self.avg_loss_per_epoch += loss_value
            if self.dnn not in ['lstm', 'lstman4']:
                acc1, = self.cal_accuracy(outputs, labels, topk=(1,))
                self.train_acc_top1.append(float(acc1))

                acc5, = self.cal_accuracy(outputs, labels, topk=(5,))
                self.train_acc_top5.append(float(acc5))
            self.train_iter += 1

            # Ahmed - End Timer of the iteration
            timer.batch_end()

            self.num_of_updates_during_comm += 1
            self.loss /= num_of_iters
            self.iter_times.append(time.time() - s)
            self.timer += time.time() - s

        display = settings.DISPLAY
        if self.train_iter % display == 0:
            logger.warning('[%3d][%5d/%5d][rank:%d] loss: %.3f, average forward (%f) and backward (%f) time: %f, iotime: %f ' %
                  (self.train_epoch, self.train_iter, self.num_batches_per_epoch, self.rank,  self.loss, self.forwardtime/display, self.backwardtime/display, self.timer/display, self.iotime/display))

            # wandb - log train accuracy and loss
            if self.rank == 0:
                self.tb.log_memory()
                #self.tb.log_trn_times(timer.batch_time.val, timer.data_time.val, self.batch_size)
                self.tb.log_trn_loss(self.loss, np.mean(self.train_acc_top1), np.mean(self.train_acc_top5))
                self.tb.log_iter_times(self.forwardtime/display, self.backwardtime/display, self.iotime/display, self.timer/display)
                self.tb.update_step_count(display)

            self.timer = 0.0
            self.iotime = 0.0
            self.forwardtime = 0.0
            self.backwardtime = 0.0

        #self.tb.update_step_count(num_of_iters)

        if self.dnn == 'lstm':
            return num_of_iters, hidden
        return num_of_iters

    def test(self, epoch):
        with torch.no_grad():
            self.net.eval()
            test_loss = 0
            correct = 0
            top1_acc = []
            top5_acc = []
            total = 0
            total_steps = 0
            costs = 0.0
            total_iters = 0
            total_wer = 0
            total_cer = 0

            # Ahmed - start timer of test iteration
            start_time = time.time()
            for batch_idx, data in enumerate(self.testloader):
                if self.dataset == 'an4':
                    inputs, labels_cpu, input_percentages, target_sizes = data
                    input_sizes = input_percentages.mul_(int(inputs.size(3))).int()
                else:
                    inputs, labels_cpu = data
                if self.is_cuda:
                    if self.dnn == 'lstm' :
                        inputs = Variable(inputs.transpose(0, 1).contiguous()).cuda()
                        labels = Variable(labels_cpu.transpose(0, 1).contiguous()).cuda()
                    else:
                        inputs, labels = inputs.cuda(non_blocking=True), labels_cpu.cuda(non_blocking=True)
                else:
                    labels = labels_cpu

                if self.dnn == 'lstm' :
                    hidden = self.net.init_hidden()
                    hidden = lstmpy.repackage_hidden(hidden)
                    outputs, hidden = self.net(inputs, hidden)
                    tt = torch.squeeze(labels.view(-1, self.net.batch_size * self.net.num_steps))
                    loss = self.criterion(outputs.view(-1, self.net.vocab_size), tt)
                    test_loss += loss.item()
                    costs += loss.item() * self.net.num_steps
                    total_steps += self.net.num_steps
                elif self.dnn == 'lstman4':
                    targets = labels_cpu
                    split_targets = []
                    offset = 0
                    for size in target_sizes:
                        split_targets.append(targets[offset:offset + size])
                        offset += size
                    out, output_sizes = self.net(inputs, input_sizes)
                    decoded_output, _ = self.decoder.decode(out.data, output_sizes)
                    target_strings = self.decoder.convert_to_strings(split_targets)
                    wer, cer = 0, 0
                    for x in range(len(target_strings)):
                        transcript, reference = decoded_output[x][0], target_strings[x][0]
                        wer += self.decoder.wer(transcript, reference) / float(len(reference.split()))
                        cer += self.decoder.cer(transcript, reference) / float(len(reference.replace(' ', '')))
                    total_wer += wer
                    total_cer += cer
                else:
                    outputs = self.net(inputs)
                    loss = self.criterion(outputs, labels)

                    acc1, acc5 = self.cal_accuracy(outputs, labels, topk=(1, 5))
                    top1_acc.append(float(acc1))
                    top5_acc.append(float(acc5))

                    test_loss += loss.data.item()
                total += labels.size(0)
                total_iters += 1

            # Ahmed - End of test iteration
            test_time = time.time() - start_time

            test_loss /= total_iters
            if self.dnn not in ['lstm', 'lstman4']:
                acc = np.mean(top1_acc)
                acc5 = np.mean(top5_acc)
            elif self.dnn == 'lstm':
                acc = np.exp(costs / total_steps)
                acc5 = 0.0
            elif self.dnn == 'lstman4':
                wer = total_wer / len(self.testloader.dataset) * 100
                cer = total_cer / len(self.testloader.dataset) * 100
                acc = wer
                acc5 = cer
            loss = float(test_loss)/total
            logger.info('Epoch %d, lr: %f, val loss: %f, val top-1 acc: %f, top-5 acc: %f' % (epoch, self.lr, test_loss, acc, acc5))

            # Ahmed - log test to wandb
            self.tb.log_eval(acc, acc5, test_time/total_iters)

            self.net.train()
        return acc

    def update_model(self):
        self.optimizer.step()

    def _get_original_params(self):
        own_state = self.net.state_dict()
        return own_state

    def remove_dict(self, dictionary):
        dictionary.clear()

    def save_checkpoint(self, state, filename):
        torch.save(state, filename)

    def _step(self, closure=None):
        """Performs a single optimization step.
            Arguments:
                closure (callable, optional): A closure that reevaluates the model and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()
    
        for group in self.optimizer.param_groups:
            weight_decay = group['weight_decay']
            momentum = group['momentum']
            dampening = group['dampening']
            nesterov = group['nesterov']
    
            for p in group['params']:
                if p.grad is None:
                    continue
                d_p = p.grad.data
                if weight_decay != 0:
                    d_p.add_(weight_decay, p.data)
                if momentum != 0:
                    param_state = self.state[p]
                    if 'momentum_buffer' not in param_state:
                        buf = param_state['momentum_buffer'] = torch.zeros_like(p.data)
                        buf.mul_(momentum).add_(d_p)
                    else:
                        buf = param_state['momentum_buffer']
                        buf.mul_(momentum).add_(1 - dampening, d_p)
                    if nesterov:
                        d_p = d_p.add(momentum, buf)
                    else:
                        d_p = buf
                p.data.add_(-group['lr'], d_p)
        return loss

    def zero_grad(self):
        self.optimizer.zero_grad()

    def log_info(self, total_time, throughput, update_time=0):
        self.tb.log('iter/update_ms', 1000 * update_time)
        self.tb.log('iter/update_tot_ratio', 1.0 * update_time / total_time)
        self.tb.log('times/train_ms', 1000 * total_time)
        #self.tb.log('iter/comm', 1000 * (total_time - np.mean(self.iter_times)))
        #self.tb.log('iter/comm_total_ratio', np.mean(self.iter_times) / total_time * 100)
        self.tb.log('throughput/1gpu_img_per_sec', throughput)
        self.tb.log('throughput/img_per_sec', throughput * self.nworkers)
        self.iter_times = []


def train_with_single(dnn, dataset, data_dir, nworkers, lr, batch_size, nsteps_update, max_epochs, num_steps=1):
    torch.cuda.set_device(0)
    trainer = DLTrainer(0, nworkers, dist=False, batch_size=batch_size, 
        is_weak_scaling=True, ngpus=1, data_dir=data_dir, dataset=dataset, 
        dnn=dnn, lr=lr, nworkers=nworkers, prefix='singlegpu', num_steps = num_steps)
    iters_per_epoch = trainer.get_num_of_training_samples() // (nworkers * batch_size * nsteps_update)

    times = []
    display = 40 if iters_per_epoch > 40 else iters_per_epoch-1
    for epoch in range(max_epochs):
        if dnn == 'lstm':
            hidden = trainer.net.init_hidden()
        for i in range(iters_per_epoch):
            s = time.time()
            trainer.optimizer.zero_grad()
            for j in range(nsteps_update):
                if dnn == 'lstm':
                    _, hidden = trainer.train(1, hidden=hidden)
                else:
                    trainer.train(1)
            trainer.update_model()
            times.append(time.time()-s)
            if i % display == 0 and i > 0: 
                time_per_iter = np.mean(times)
                throughput = batch_size * nsteps_update / time_per_iter
                logger.info('Time per iteration including communication: %f. Speed: %f images/s', time_per_iter, batch_size * nsteps_update / time_per_iter)
                trainer.log_info(time_per_iter, throughput)
                times = []


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Single trainer")
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--nsteps-update', type=int, default=1)
    parser.add_argument('--dataset', type=str, default='imagenet', choices=_support_datasets, help='Specify the dataset for training')
    parser.add_argument('--dnn', type=str, default='resnet50', choices=_support_dnns, help='Specify the neural network for training')
    parser.add_argument('--data-dir', type=str, default='./data', help='Specify the data root path')
    parser.add_argument('--lr', type=float, default=0.1, help='Default learning rate')
    parser.add_argument('--max-epochs', type=int, default=settings.MAX_EPOCHS, help='Default maximum epochs to train')
    parser.add_argument('--num-steps', type=int, default=35)
    parser.add_argument('--projname', type=str, default='test')
    parser.add_argument('--name', type=str, default='testing', help="name of the current run, used for machine naming and tensorboard visualization")
    parser.add_argument('--netdevice', type=str, default='ens1f0')

    args = parser.parse_args()
    batch_size = args.batch_size * args.nsteps_update
    prefix = settings.PREFIX
    relative_path = './logs/singlegpu-%s/%s-n%d-bs%d-lr%.4f-ns%d' % (prefix, args.dnn, 1, batch_size, args.lr, args.nsteps_update)
    utils.create_path(relative_path)
    logfile = os.path.join(relative_path, settings.hostname+'.log')
    hdlr = logging.FileHandler(logfile)
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr) 
    logger.info('Configurations: %s', args)

    #Wandb and tensorboard logging
    is_master = (os.environ.get('RANK', '0') == '0')
    if args.projname != 'test':
        #initialize WANDB
        if not is_master:
            os.environ['WANDB_MODE'] = 'dryrun'  # all wandb.log are no-op
            logger.info("local-only wandb logging for run " + args.name)
        group_name = args.name
        run_name = args.name + '-' + os.environ.get("RANK", "0")
        wandb.init(project=args.projname, group=group_name, name=run_name)
        logger.info("initializing wandb logging to group " + args.name + " name ")
    tb = TensorboardLogger(relative_path, is_master=is_master)
    #log = FileLogger(args.logdir, is_master=is_master, is_rank0=is_master)

    train_with_single(args.dnn, args.dataset, args.data_dir, 1, args.lr, args.batch_size, args.nsteps_update, args.max_epochs, args.num_steps, tb=tb)
