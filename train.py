import os
import random
import time
import cv2
import numpy as np
import logging
import argparse

import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torch.multiprocessing as mp
import torch.distributed as dist
from tensorboardX import SummaryWriter

from model.PFENet import PFENet
from util import dataset
from util import transform, config
from util.util import AverageMeter, poly_learning_rate, intersectionAndUnionGPU, intersectionAndUnion

cv2.ocl.setUseOpenCL(False)
cv2.setNumThreads(0)
filename = 'PFENet.pth' # name for best model


def get_parser():
    parser = argparse.ArgumentParser(description='PyTorch Semantic Segmentation')
    parser.add_argument('--config', type=str, default='config/ade20k/ade20k_pspnet50.yaml', help='config file')
    parser.add_argument('opts', help='see config/ade20k/ade20k_pspnet50.yaml for all options', default=None,
                        nargs=argparse.REMAINDER)
    args = parser.parse_args()
    assert args.config is not None
    cfg = config.load_cfg_from_cfg_file(args.config)
    if args.opts is not None:
        cfg = config.merge_cfg_from_list(cfg, args.opts)
    return cfg


def get_logger():
    logger_name = "main-logger"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    fmt = "[%(asctime)s line %(lineno)d %(process)d] %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    return logger



def main():
    global args
    args = get_parser()
    assert args.classes > 1
    assert args.zoom_factor in [1, 2, 4, 8]
    assert (args.train_h - 1) % 8 == 0 and (args.train_w - 1) % 8 == 0
    if args.manual_seed is not None:
        np.random.seed(args.manual_seed)
        torch.manual_seed(args.manual_seed)
        random.seed(args.manual_seed)
        if args.cuda:
            torch.cuda.manual_seed_all(args.manual_seed)
            cudnn.benchmark = False
            cudnn.deterministic = True
            torch.cuda.manual_seed(args.manual_seed)


    BatchNorm = nn.BatchNorm2d
    criterion = nn.CrossEntropyLoss(ignore_index=args.ignore_label)

    model = PFENet(layers=args.layers, classes=2, zoom_factor=8, \
                   criterion=nn.CrossEntropyLoss(ignore_index=255), BatchNorm=BatchNorm, \
                   pretrained=True, shot=args.shot, ppm_scales=args.ppm_scales, vgg=args.vgg)   # if arg.vgg=False then use Resnet
    global device
    device = torch.device("cuda:0" if args.cuda else "cpu")
    model = model.to(device)

    for param in model.layer0.parameters():     #################################################### param and optimizer
        param.requires_grad = False
    for param in model.layer1.parameters():
        param.requires_grad = False
    for param in model.layer2.parameters():
        param.requires_grad = False
    for param in model.layer3.parameters():
        param.requires_grad = False
    for param in model.layer4.parameters():
        param.requires_grad = False

    optimizer = torch.optim.SGD(
        [
            {'params': model.down_query.parameters()},
            {'params': model.down_supp.parameters()},
            {'params': model.init_merge.parameters()},
            {'params': model.alpha_conv.parameters()},
            {'params': model.beta_conv.parameters()},
            {'params': model.inner_cls.parameters()},
            {'params': model.res1.parameters()},
            {'params': model.res2.parameters()},
            {'params': model.cls.parameters()}],
        lr=args.base_lr, momentum=args.momentum, weight_decay=args.weight_decay)

    global logger, writer        ######################################################################### write the log
    logger = get_logger()
    writer = SummaryWriter(args.save_path)
    logger.info("=> creating model ...")
    logger.info("Classes: {}".format(args.classes))
    logger.info(model)
    print(args)

    if args.weight:     ############################################################### load pretrained weight or resume
        if os.path.isfile(args.weight):
            logger.info("=> loading weight '{}'".format(args.weight))
            checkpoint = torch.load(args.weight)
            model.load_state_dict(checkpoint['state_dict'])
            logger.info("=> loaded weight '{}'".format(args.weight))
        else:
            logger.info("=> no weight found at '{}'".format(args.weight))

    if args.resume:
        if os.path.isfile(args.resume):
            logger.info("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume, map_location=lambda storage, loc: storage.cuda())
            args.start_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            logger.info("=> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint['epoch']))
        else:
            logger.info("=> no checkpoint found at '{}'".format(args.resume))

    value_scale = 255   ############################################################## load train/val data and transform
    mean = [0.485, 0.456, 0.406]
    mean = [item * value_scale for item in mean]
    std = [0.229, 0.224, 0.225]
    std = [item * value_scale for item in std]

    assert args.split in [0, 1, 2, 3, 999]
    train_transform = [
        transform.RandScale([args.scale_min, args.scale_max]),
        transform.RandRotate([args.rotate_min, args.rotate_max], padding=mean, ignore_label=args.padding_label), # padding为mean, 会在之后的归一化中变为0
        transform.RandomGaussianBlur(),
        transform.RandomHorizontalFlip(),
        transform.Crop([args.train_h, args.train_w], crop_type='rand', padding=mean, ignore_label=args.padding_label),
        transform.ToTensor(),                     # 自定义，没有归一化到0～1
        transform.Normalize(mean=mean, std=std)]  # 现在应该是归一化到 -1到1 之间
    train_transform = transform.Compose(train_transform)
    train_data = dataset.SemData(split=args.split, shot=args.shot, data_root=args.data_root, \
                                 data_list=args.train_list, transform=train_transform, mode='train', \
                                 use_coco=args.use_coco, use_split_coco=args.use_split_coco)

    train_sampler = None
    kwargs = {'num_workers': args.workers, 'pin_memory': True} if args.cuda else {}
    train_loader = torch.utils.data.DataLoader(train_data, batch_size=args.batch_size, shuffle=(train_sampler is None),
                                               sampler=train_sampler, drop_last=True, **kwargs
                                               )   # 每个episode为一个样本， 一个batch为多个episodes
    if args.evaluate:
        if args.resized_val:
            val_transform = transform.Compose([
                transform.Resize(size=args.val_size),
                transform.ToTensor(),
                transform.Normalize(mean=mean, std=std)])
        else:
            val_transform = transform.Compose([
                transform.test_Resize(size=args.val_size),
                transform.ToTensor(),
                transform.Normalize(mean=mean, std=std)])
        # val 数据用 val_list.txt(从val数据中选择），其class从sub_val_list中选择，与训练数据不能重合
        val_data = dataset.SemData(split=args.split, shot=args.shot, data_root=args.data_root, \
                                   data_list=args.val_list, transform=val_transform, mode='val', \
                                   use_coco=args.use_coco, use_split_coco=args.use_split_coco)       # 用 val_list.txt
        val_sampler = None
        val_loader = torch.utils.data.DataLoader(val_data, batch_size=args.batch_size_val, shuffle=False,
                                                 sampler=val_sampler, **kwargs)


    max_iou = 0.        ######################################################################################## 开始训练

    for epoch in range(args.start_epoch, args.epochs):
        if args.fix_random_seed_val:
            np.random.seed(args.manual_seed + epoch)              ########################################################## 为什么要两次set seed, 而且每个epoch都要重新选
            random.seed(args.manual_seed + epoch)
            torch.manual_seed(args.manual_seed + epoch)
            if args.cuda:
                torch.cuda.manual_seed(args.manual_seed + epoch)
                torch.cuda.manual_seed_all(args.manual_seed + epoch)

        epoch_log = epoch + 1
        loss_train, mIoU_train, mAcc_train, allAcc_train = train(train_loader, model, optimizer, epoch, args)

        writer.add_scalar('loss_train', loss_train, epoch_log)
        writer.add_scalar('mIoU_train', mIoU_train, epoch_log)
        writer.add_scalar('mAcc_train', mAcc_train, epoch_log)
        writer.add_scalar('allAcc_train', allAcc_train, epoch_log)

        if args.evaluate and (epoch % 2 == 0 or (args.epochs <= 50 and epoch % 1 == 0)):
            loss_val, mIoU_val, mAcc_val, allAcc_val, class_miou = validate(val_loader, model, criterion)

            writer.add_scalar('loss_val', loss_val, epoch_log)
            writer.add_scalar('mIoU_val', mIoU_val, epoch_log)
            writer.add_scalar('mAcc_val', mAcc_val, epoch_log)
            writer.add_scalar('class_miou_val', class_miou, epoch_log)
            writer.add_scalar('allAcc_val', allAcc_val, epoch_log)
            if class_miou > max_iou:
                max_iou = class_miou
#                if os.path.exists(filename):
#                    os.remove(filename)
                filename = args.save_path + '/train_epoch_' + str(epoch) + '_' + str(max_iou) + '.pth'
                logger.info('Saving checkpoint to: ' + filename)
                torch.save({'epoch': epoch, 'state_dict': model.state_dict(), 'optimizer': optimizer.state_dict()},
                           filename)

    filename = args.save_path + '/final.pth'
    logger.info('Saving checkpoint to: ' + filename)
    torch.save({'epoch': args.epochs, 'state_dict': model.state_dict(), 'optimizer': optimizer.state_dict()}, filename)


def train(train_loader, model, optimizer, epoch, args):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    main_loss_meter = AverageMeter()
    aux_loss_meter = AverageMeter()
    loss_meter = AverageMeter()
    intersection_meter = AverageMeter()
    union_meter = AverageMeter()
    target_meter = AverageMeter()

    model.train()
    end = time.time()
    max_iter = args.epochs * len(train_loader)     # 所有epoch 总共多少iter
    print('Warmup: {}'.format(args.warmup))
    for i, (input, target, s_input, s_mask, subcls) in enumerate(train_loader):
        # input [B, 3, 473, 473], target:[B, 473, 473], s_input:[B, K, 3, 473, 473], s_mask:[B,K,473,473], subcls[list of cls w.r.t. B samples]
        data_time.update(time.time() - end)
        current_iter = epoch * len(train_loader) + i + 1
        index_split = -1
        if args.base_lr > 1e-6:                                                  # decay the learning rate in each batch
            poly_learning_rate(optimizer, args.base_lr, current_iter, max_iter, power=args.power,
                               index_split=index_split, warmup=args.warmup, warmup_step=len(train_loader) // 2)

        if device.type == 'cuda':
            s_input = s_input.cuda(non_blocking=True)
            s_mask = s_mask.cuda(non_blocking=True)
            input = input.cuda(non_blocking=True)
            target = target.cuda(non_blocking=True)

        output, main_loss, aux_loss = model(s_x=s_input, s_y=s_mask, x=input, y=target)

        if not args.multiprocessing_distributed:
            main_loss, aux_loss = torch.mean(main_loss), torch.mean(aux_loss)              ##################################
        loss = main_loss + args.aux_weight * aux_loss
        optimizer.zero_grad()

        loss.backward()
        optimizer.step()
        n = input.size(0)   # batch_size
        if args.multiprocessing_distributed:
            main_loss, aux_loss, loss = main_loss.detach() * n, aux_loss * n, loss * n
            count = target.new_tensor([n], dtype=torch.long)
            dist.all_reduce(main_loss), dist.all_reduce(aux_loss), dist.all_reduce(loss), dist.all_reduce(count)
            n = count.item()
            main_loss, aux_loss, loss = main_loss / n, aux_loss / n, loss / n

        intersection, union, target = intersectionAndUnionGPU(output, target, args.classes, args.ignore_label)
        if args.multiprocessing_distributed:
            dist.all_reduce(intersection), dist.all_reduce(union), dist.all_reduce(target)
        intersection, union, target = intersection.cpu().numpy(), union.cpu().numpy(), target.cpu().numpy()
        intersection_meter.update(intersection), union_meter.update(union), target_meter.update(target)

        accuracy = sum(intersection_meter.val) / (sum(target_meter.val) + 1e-10)  # inter_meter [num0, num1], target_meter[num0, num1]
        main_loss_meter.update(main_loss.item(), n)
        aux_loss_meter.update(aux_loss.item(), n)
        loss_meter.update(loss.item(), n)
        batch_time.update(time.time() - end)    #跑完batch所需要时间
        end = time.time()

        remain_iter = max_iter - current_iter
        remain_time = remain_iter * batch_time.avg
        t_m, t_s = divmod(remain_time, 60)
        t_h, t_m = divmod(t_m, 60)
        remain_time = '{:02d}:{:02d}:{:02d}'.format(int(t_h), int(t_m), int(t_s))

        if (i + 1) % args.print_freq == 0:
            logger.info('Epoch: [{}/{}][{}/{}] '
                        'Data {data_time.val:.3f} ({data_time.avg:.3f}) '
                        'Batch {batch_time.val:.3f} ({batch_time.avg:.3f}) '
                        'Remain {remain_time} '
                        'MainLoss {main_loss_meter.val:.4f}'    # 当前batch loss 
                        'AuxLoss {aux_loss_meter.val:.4f} '     
                        'Loss {loss_meter.val:.4f} '
                        'Accuracy {accuracy:.4f}.'.format(epoch + 1, args.epochs, i + 1, len(train_loader),
                                                          batch_time=batch_time,
                                                          data_time=data_time,
                                                          remain_time=remain_time,
                                                          main_loss_meter=main_loss_meter,
                                                          aux_loss_meter=aux_loss_meter,
                                                          loss_meter=loss_meter,
                                                          accuracy=accuracy))

        writer.add_scalar('loss_train_batch', main_loss_meter.val, current_iter)  # 当前batch loss
        writer.add_scalar('mIoU_train_batch', np.mean(intersection / (union + 1e-10)), current_iter)   # BG的IoU与FG的IoU平均
        writer.add_scalar('mAcc_train_batch', np.mean(intersection / (target + 1e-10)), current_iter)  # ClassWise Acc 平均
        writer.add_scalar('allAcc_train_batch', accuracy, current_iter)

    iou_class = intersection_meter.sum / (union_meter.sum + 1e-10)   # [BG_IoU, FG_IoU]
    accuracy_class = intersection_meter.sum / (target_meter.sum + 1e-10)  #[FG_Acc, BG_Acc]
    mIoU = np.mean(iou_class)
    mAcc = np.mean(accuracy_class)                                          # ClassWise Acc的平均
    allAcc = sum(intersection_meter.sum) / (sum(target_meter.sum) + 1e-10)  # 整体Acc

    logger.info('Train result at epoch [{}/{}]: mIoU/mAcc/allAcc {:.4f}/{:.4f}/{:.4f}.'.format(
        epoch, args.epochs, mIoU, mAcc, allAcc))
    for i in range(args.classes):
        logger.info('Class_{} Result: iou/accuracy {:.4f}/{:.4f}.'.format(i, iou_class[i], accuracy_class[i]))
    return main_loss_meter.avg, mIoU, mAcc, allAcc


def validate(val_loader, model, criterion):

    logger.info('>>>>>>>>>>>>>>>> Start Evaluation >>>>>>>>>>>>>>>>')
    batch_time = AverageMeter()
    model_time = AverageMeter()
    data_time = AverageMeter()
    loss_meter = AverageMeter()
    intersection_meter = AverageMeter()
    union_meter = AverageMeter()
    target_meter = AverageMeter()
    if args.use_coco:
        split_gap = 20
    else:
        split_gap = 5
    class_intersection_meter = [0] * split_gap
    class_union_meter = [0] * split_gap

    if args.manual_seed is not None and args.fix_random_seed_val:
        random.seed(args.manual_seed)
        np.random.seed(args.manual_seed)
        torch.manual_seed(args.manual_seed)
        if args.cuda:
            torch.cuda.manual_seed(args.manual_seed)
            torch.cuda.manual_seed_all(args.manual_seed)


    model.eval()
    end = time.time()
    if args.split != 999:
        if args.use_coco:
            test_num = 20000
        else:
            test_num = 5000
    else:
        test_num = len(val_loader)
    assert test_num % args.batch_size_val == 0
    iter_num = 0
    for e in range(10):
        for i, (input, target, s_input, s_mask, subcls, ori_label) in enumerate(val_loader):
            # input[1,3,473,473],target[1,473,473],s_input[1,1,3,473,473],s_mask[1,1,473,473], ori_label:[1,366,500]
            # val batch_size为1
            if (iter_num - 1) * args.batch_size_val >= test_num:
                break
            iter_num += 1
            data_time.update(time.time() - end)
            if device.type == 'cuda':
                input = input.cuda(non_blocking=True)
                target = target.cuda(non_blocking=True)
                ori_label = ori_label.cuda(non_blocking=True)
                s_input = s_input.cuda(non_blocking=True)
                s_mask = s_mask.cuda(non_blocking=True)                                     # 为什么这里之前没有 转化为 cuda

            start_time = time.time()
            output = model(s_x=s_input, s_y=s_mask, x=input, y=target)    # [B(1), 2, 473, 473]  是logit
            model_time.update(time.time() - start_time)

            if args.ori_resize:       # 不用dataloader里的target, 而用ori_label, 并pad为方形
                longerside = max(ori_label.size(1), ori_label.size(2))   # ori_label:[1, h, w], label为0， 1， 255
                backmask = torch.ones(ori_label.size(0), longerside, longerside)  #[1, l, l]
                if device.type == 'cuda':
                    backmask = backmask.cuda()*255
                else:
                    backmask = backmask*255

                backmask[0, :ori_label.size(1), :ori_label.size(2)] = ori_label  # 有效的mask，其他的为255
                target = backmask.clone().long()                                 # target为方形，对原图像没有rescale

            output = F.interpolate(output, size=target.size()[1:], mode='bilinear', align_corners=True)
            loss = criterion(output, target)  # CELoss pred/output为[B,c, h, w], GT为[B,h,w]

            loss = torch.mean(loss)                      # 单个图片loss                                                # 没有必要

            output = output.max(1)[1]     # [B, h, w] 得到每个pixel对应class index

            intersection, union, new_target = intersectionAndUnionGPU(output, target, args.classes, args.ignore_label)
            intersection, union, target, new_target = intersection.cpu().numpy(), union.cpu().numpy(), target.cpu().numpy(), new_target.cpu().numpy()
            intersection_meter.update(intersection), union_meter.update(union), target_meter.update(new_target)

            subcls = subcls[0].cpu().numpy()[0]    #其实每个iteration只针对一张query image,K个support img, len(subcls)=K, 里面只有一个class
            class_intersection_meter[(subcls - 1) % split_gap] += intersection[1]   # intersection[1]是针对FG
            class_union_meter[(subcls - 1) % split_gap] += union[1]                 # union中的fg

            accuracy = sum(intersection_meter.val) / (sum(target_meter.val) + 1e-10)   # 累计的ACC
            loss_meter.update(loss.item(), input.size(0))
            batch_time.update(time.time() - end)
            end = time.time()
            if ((i + 1) % (test_num / 100) == 0):
                logger.info('Test: [{}/{}] '
                            'Data {data_time.val:.3f} ({data_time.avg:.3f}) '
                            'Batch {batch_time.val:.3f} ({batch_time.avg:.3f}) '
                            'Loss {loss_meter.val:.4f} ({loss_meter.avg:.4f}) '
                            'Accuracy {accuracy:.4f}.'.format(iter_num * args.batch_size_val, test_num,
                                                              data_time=data_time,
                                                              batch_time=batch_time,
                                                              loss_meter=loss_meter,
                                                              accuracy=accuracy))

    iou_class = intersection_meter.sum / (union_meter.sum + 1e-10)
    accuracy_class = intersection_meter.sum / (target_meter.sum + 1e-10)
    mIoU = np.mean(iou_class)
    mAcc = np.mean(accuracy_class)
    allAcc = sum(intersection_meter.sum) / (sum(target_meter.sum) + 1e-10)

    class_iou_class = []
    class_miou = 0
    for i in range(len(class_intersection_meter)):    # 每个class的总Intersection和Union和IoU
        class_iou = class_intersection_meter[i] / (class_union_meter[i] + 1e-10)
        class_iou_class.append(class_iou)
        class_miou += class_iou
    class_miou = class_miou * 1.0 / len(class_intersection_meter)
    logger.info('meanIoU---Val result: mIoU {:.4f}.'.format(class_miou))   #每个class IoU然后取平均
    for i in range(split_gap):
        logger.info('Class_{} Result: iou {:.4f}.'.format(i + 1, class_iou_class[i]))


    logger.info('FBIoU---Val result: mIoU/mAcc/allAcc {:.4f}/{:.4f}/{:.4f}.'.format(mIoU, mAcc, allAcc))
    for i in range(args.classes):
        logger.info('Class_{} Result: iou/accuracy {:.4f}/{:.4f}.'.format(i, iou_class[i], accuracy_class[i]))
    logger.info('<<<<<<<<<<<<<<<<< End Evaluation <<<<<<<<<<<<<<<<<')

    print('avg inference time: {:.4f}, count: {}'.format(model_time.avg, test_num))
    return loss_meter.avg, mIoU, mAcc, allAcc, class_miou


if __name__ == '__main__':
    main()
