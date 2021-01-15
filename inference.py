import os
import numpy as np
import argparse
import time
import pickle
import cv2
import sys
import torch
import torch.nn as nn
from torch.autograd import Variable
from tqdm import tqdm
from matplotlib import pyplot as plt

from roi_data_layer.roidb import combined_roidb
from roi_data_layer.inference_loader import InferenceLoader
from roi_data_layer.general_test_loader import GeneralTestLoader
from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.rpn.bbox_transform import clip_boxes
from model.roi_layers import nms
from model.rpn.bbox_transform import bbox_transform_inv
from model.utils.net_utils import save_net, load_net, vis_detections

from model.utils.fsod_logger import FSODInferenceLogger

from model.framework.faster_rcnn import FasterRCNN
from model.framework.fsod import FSOD
from model.framework.meta import METARCNN
from model.framework.fgn import FGN
from model.framework.dana import DAnARCNN
from model.framework.cisa import CISARCNN



def parse_args():
    """
    Parse input arguments
    """
    parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')
    parser.add_argument('--dataset', dest='dataset',
                        help='training dataset',
                        default='pascal_voc', type=str)
    parser.add_argument('--net', dest='net',
                        help='vgg16, res101',
                        default='res50', type=str)
    parser.add_argument('--start_epoch', dest='start_epoch',
                        help='starting epoch',
                        default=1, type=int)
    parser.add_argument('--epochs', dest='max_epochs',
                        help='number of epochs to train',
                        default=12, type=int)
    parser.add_argument('--disp_interval', dest='disp_interval',
                        help='number of iterations to display',
                        default=100, type=int)
    parser.add_argument('--checkpoint_interval', dest='checkpoint_interval',
                        help='number of iterations to display',
                        default=10000, type=int)
    parser.add_argument('--save_dir', dest='save_dir',
                        help='directory to save models', default="models",
                        type=str)
    parser.add_argument('--nw', dest='num_workers',
                        help='number of worker to load data',
                        default=8, type=int)
    parser.add_argument('--ls', dest='large_scale',
                        help='whether use large imag scale',
                        action='store_true')                      
    parser.add_argument('--mGPUs', dest='mGPUs',
                        help='whether use multiple GPUs',
                        action='store_true')
    parser.add_argument('--bs', dest='batch_size',
                        help='batch_size',
                        default=16, type=int)
                        
    parser.add_argument('--fs', dest='few_shot',
                        help='whether under the few-shot paradigm',
                        default=True)
    parser.add_argument('--way', dest='way',
                        help='num of support way',
                        default=2, type=int)
    parser.add_argument('--shot', dest='shot',
                        help='num of support shot',
                        default=5, type=int)
    parser.add_argument('--flip', dest='use_flip',
                        help='use flipped data or not',
                        default=False, action='store_true')

    # config optimization
    parser.add_argument('--o', dest='optimizer',
                        help='training optimizer',
                        default="sgd", type=str)
    parser.add_argument('--lr', dest='lr',
                        help='starting learning rate',
                        default=0.001, type=float)
    parser.add_argument('--lr_decay_step', dest='lr_decay_step',
                        help='step to do learning rate decay, unit is epoch',
                        default=5, type=int)
    parser.add_argument('--lr_decay_gamma', dest='lr_decay_gamma',
                        help='learning rate decay ratio',
                        default=0.1, type=float)

    # resume trained model
    parser.add_argument('--r', dest='resume',
                        help='resume checkpoint or not',
                        action='store_true', default=False)
    parser.add_argument('--load_dir', dest='load_dir',
                        help='directory to load models', default="models",
                        type=str)
    parser.add_argument('--checkepoch', dest='checkepoch',
                        help='checkepoch to load model',
                        default=1, type=int)
    parser.add_argument('--checkpoint', dest='checkpoint',
                        help='checkpoint to load model',
                        default=0, type=int)
    parser.add_argument('--eof', dest='evaluation_output_folder',
                        help='output folder of evaluation files',
                        default='tmp', type=str)
    parser.add_argument('--sp_dir', dest='support_dir',
                        help='directory of support images',
                        default='all', type=str)
    parser.add_argument('--lsi', dest='log_save_im',
                        help='save im in logger',
                        default=False, action='store_true')

    args = parser.parse_args()
    return args


if __name__ == '__main__':
    TOTAL_DEC_TIME = 0

    args = parse_args()
    print('Called with args:')
    print(args)

    np.random.seed(cfg.RNG_SEED)

    args.set_cfgs = ['ANCHOR_SCALES', '[4, 8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
    if args.dataset == "val2014_novel":
        args.imdbval_name = "coco_20_set1"
    elif args.dataset == "val2014_base":
        args.imdbval_name = "coco_20_set2"
    elif args.dataset == "ycb2d":
        args.imdbval_name = "ycb2d_inference"
    elif args.dataset == "dense_ycb2d":
        args.imdbval_name = "ycb2d_dense_inference"
    else:
        raise Exception("dataset is not defined")

    args.cfg_file = "cfgs/res50.yml"
    cfg_from_file(args.cfg_file)
    cfg_from_list(args.set_cfgs)

    # prepare roidb
    cfg.TRAIN.USE_FLIPPED = False
    imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbval_name, False)
    CWD = os.getcwd()
    support_dir = os.path.join(CWD, 'data/supports', args.support_dir)

    # load dir
    input_dir = os.path.join(args.load_dir, "train/checkpoints")
    if not os.path.exists(input_dir):
        raise Exception('There is no input directory for loading network from ' + input_dir)
    load_name = os.path.join(input_dir,
        'model_{}_{}.pth'.format(args.checkepoch, args.checkpoint))

    # initilize the network
    if args.net == 'frcnn':
        model = FasterRCNN(imdb.classes, pretrained=False)
    elif args.net == 'fsod':
        model = FSOD(imdb.classes, pretrained=False, num_way=args.way, num_shot=args.shot)
    elif args.net == 'meta':
        model = METARCNN(imdb.classes, pretrained=False, num_way=args.way, num_shot=args.shot)
    elif args.net == 'fgn':
        model = FGN(imdb.classes, pretrained=False, num_way=args.way, num_shot=args.shot)
    elif args.net == 'cisa':
        model = CISARCNN(imdb.classes, 'concat', 256, 256, pretrained=False, num_way=args.way, num_shot=args.shot)
    else:
        raise Exception(f"network {args.net} is not defined")

    model.create_architecture()
    print("load checkpoint %s" % (load_name))
    checkpoint = torch.load(load_name)
    model.load_state_dict(checkpoint['model'])
    if args.mGPUs:
        model = model.module
    if 'pooling_mode' in checkpoint.keys():
        cfg.POOLING_MODE = checkpoint['pooling_mode']
    print('load model successfully!')
    cfg.CUDA = True
    model.cuda()

    # initilize the tensor holders
    im_data = torch.FloatTensor(1)
    im_info = torch.FloatTensor(1)
    num_boxes = torch.LongTensor(1)
    gt_boxes = torch.FloatTensor(1)

    im_data = im_data.cuda()
    im_info = im_info.cuda()
    num_boxes = num_boxes.cuda()
    gt_boxes = gt_boxes.cuda()

    im_data = Variable(im_data)
    im_info = Variable(im_info)
    num_boxes = Variable(num_boxes)
    gt_boxes = Variable(gt_boxes)

    if args.few_shot:
        support_ims = torch.FloatTensor(1)
        support_ims = support_ims.cuda()
        support_ims = Variable(support_ims)

    # prepare holder for predicted boxes
    start = time.time()
    max_per_image = 100
    thresh = 0.05
    num_images = len(imdb.image_index)
    all_boxes = [[[] for _ in range(num_images)]
                for _ in range(imdb.num_classes)]
    _t = {'im_detect': time.time(), 'misc': time.time()}

    model.eval()
    empty_array = np.transpose(np.array([[],[],[],[],[]]), (1,0))

    imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbval_name, False)
    imdb.competition_mode(on=True)
    if args.few_shot:
        dataset = InferenceLoader(0, imdb, roidb, ratio_list, ratio_index, support_dir, 
                            1, len(imdb._classes), num_shot=args.shot, training=False, normalize=False)
    else:
        dataset = GeneralTestLoader(roidb, ratio_list, ratio_index, 1, \
                                81, training=False, normalize = False)
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=1,
                                shuffle=False, num_workers=0, pin_memory=True)
    data_iter = iter(dataloader)

    for i in tqdm(range(num_images)):
        data = next(data_iter)
        with torch.no_grad():
            im_data.resize_(data[0].size()).copy_(data[0])
            im_info.resize_(data[1].size()).copy_(data[1])
            gt_boxes.resize_(data[2].size()).copy_(data[2])
            num_boxes.resize_(data[3].size()).copy_(data[3])
            if args.few_shot:
                support_ims.resize_(data[4].size()).copy_(data[4])
            else:
                support_ims = None

        det_tic = time.time()
        with torch.no_grad():
            rois, cls_prob, bbox_pred, \
            rpn_loss_cls, rpn_loss_box, \
            RCNN_loss_cls, RCNN_loss_bbox, \
            rois_label = model(im_data, im_info, gt_boxes, num_boxes, support_ims)
        det_toc = time.time()
        detect_time = det_toc - det_tic
        TOTAL_DEC_TIME += detect_time
        misc_tic = time.time()

        scores = cls_prob.data
        boxes = rois.data[:, :, 1:5]

        # Apply bounding-box regression deltas
        box_deltas = bbox_pred.data
        if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
        # Optionally normalize targets by a precomputed mean and stdev

            box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                    + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
            box_deltas = box_deltas.view(1, -1, 4)


        pred_boxes = bbox_transform_inv(boxes, box_deltas, 1)
        pred_boxes = clip_boxes(pred_boxes, im_info.data, 1)

        # re-scale boxes to the origin img scale
        pred_boxes /= data[1][0][2].item()

        scores = scores.squeeze()
        pred_boxes = pred_boxes.squeeze()
        selected_class = gt_boxes[0, 0, 4]

        for j in range(1, imdb.num_classes):
            if j != selected_class:
                all_boxes[j][i] = empty_array
                continue
            inds = torch.nonzero(scores[:,1]>thresh).view(-1)
            # if there is det
            if inds.numel() > 0:
                cls_scores = scores[:,1][inds]
                _, order = torch.sort(cls_scores, 0, True)

                cls_boxes = pred_boxes[inds, :]

        
                cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1)
                cls_dets = cls_dets[order]
                keep = nms(cls_boxes[order, :], cls_scores[order], cfg.TEST.NMS)
                cls_dets = cls_dets[keep.view(-1).long()]
                all_boxes[j][i] = cls_dets.cpu().numpy()

                
        misc_toc = time.time()
        nms_time = misc_toc - misc_tic
        if args.log_save_im:
            origin_im = im_data[0].permute(1, 2, 0).contiguous().cpu().numpy()[:, :, ::-1]
            origin_im = origin_im - origin_im.min()
            origin_im /= origin_im.max()
            gt_im = origin_im.copy()
            pt_im = origin_im.copy()
            np_gt_boxes = gt_boxes[0]
            for n in range(np_gt_boxes.shape[0]):
                box = np_gt_boxes[n].clone()
                cv2.rectangle(gt_im, (box[0], box[1]), (box[2], box[3]), (0.1, 1, 0.1), 2)
            plt.imshow(gt_im)
            plt.show()
            for n in range(cls_dets.shape[0]):
                box = cls_dets[n].clone() * data[1][0][2].item()
                if box[4] < 0.8:
                    continue
                cv2.rectangle(pt_im, (box[0], box[1]), (box[2], box[3]), (0.1, 1, 0.1), 2)
            plt.imshow(pt_im)
            plt.show()
            # raise Exception(' ')
            # cv2.rectangle(im, (box[0], box[1]), (box[0] + box[2], box[1] + box[3]), (20, 255, 20), 2)
            # tb_logger.write(i, gt, support_ims, predict, save_im=True)

    sys.stdout.write('im_detect: {:d}/{:d} {:.3f}s {:.3f}s   \r' \
            .format(i + 1, num_images, detect_time, nms_time))
    sys.stdout.flush()

    output_dir = os.path.join(CWD, 'inference_output', args.evaluation_output_folder)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    det_file = os.path.join(output_dir, 'detections.pkl')
    with open(det_file, 'wb') as f:
        pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)
    print('Evaluating detections')
    imdb.evaluate_detections(all_boxes, output_dir)
