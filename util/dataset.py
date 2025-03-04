# encoding:utf-8
import os
import os.path
import cv2
import numpy as np

from torch.utils.data import Dataset
import torch.nn.functional as F
import torch
import random
import time
from tqdm import tqdm
from .transform import Compose, FitCrop, RandScale, ColorJitter

IMG_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.ppm', '.bmp', '.pgm']



def is_image_file(filename):
    filename_lower = filename.lower()
    return any(filename_lower.endswith(extension) for extension in IMG_EXTENSIONS)


def make_dataset(split=0, data_root=None, data_list=None, sub_list=None):    # data_list: query set. sub_list: support cls list
    # data_list 应该是所有图片数据 （meta train过程用train_list, meta_test过程用val_list)
    # scan所有的训练数据， 找到sub_list中的class与其多对应的image(img_path, label_path)
    assert split in [0, 1, 2, 3, 10, 11, 999]
    if not os.path.isfile(data_list):
        raise (RuntimeError("Image list file do not exist: " + data_list + "\n"))

    # Shaban uses these lines to remove small objects:
    # if util.change_coordinates(mask, 32.0, 0.0).sum() > 2:
    #    filtered_item.append(item)      
    # which means the mask will be downsampled to 1/32 of the original size and the valid area should be larger than 2, 
    # therefore the area in original size should be accordingly larger than 2 * 32 * 32    
    image_label_list = []  
    list_read = open(data_list).readlines()
    print("Processing data...".format(sub_list))
    sub_class_file_list = {}
    for sub_c in sub_list:
        sub_class_file_list[sub_c] = []    # 每个class对应的image （img path, label path)

    for l_idx in tqdm(range(len(list_read))):
        line = list_read[l_idx]
        line = line.strip()
        line_split = line.split(' ')  # 分别得到 image 和 mask的路径
        image_name = os.path.join(data_root, line_split[0])
        label_name = os.path.join(data_root, line_split[1])
        item = (image_name, label_name)
        label = cv2.imread(label_name, cv2.IMREAD_GRAYSCALE)
        label_class = np.unique(label).tolist()      # 当前图片所有的label/class

        if 0 in label_class:
            label_class.remove(0)
        if 255 in label_class:
            label_class.remove(255)

        # 当前图片的所有cls, 如果满足条件 (在sub_list中，且图片中有大于2*32*32个pixel),则把当前图片加入image_label_list, 并相应的更新 sub_class_file_list[c]
        new_label_class = []       
        for c in label_class:
            if c in sub_list:
                tmp_label = np.zeros_like(label)
                target_pix = np.where(label == c)
                tmp_label[target_pix[0],target_pix[1]] = 1   # 标注出class为c的pixel(其值设为1）
                if tmp_label.sum() >= 2 * 32 * 32:      
                    new_label_class.append(c)

        label_class = new_label_class      # 当前图片所有符合条件的cls

        if len(label_class) > 0:
            image_label_list.append(item)   # 符合条件的image的 图片path + label path
            for c in label_class:
                if c in sub_list:
                    sub_class_file_list[c].append(item)     # 所有跟cls c相关的图片
                    
    print("Checking image&label pair {} list done! ".format(split))
    return image_label_list, sub_class_file_list
    # image_label_list: list of 所有(image path, mask_path)
    # sub_class_file_list： {c: list of 所有跟cls c相关的 （图片+其label path）}



class SemData(Dataset):
    def __init__(self, split=3, shot=1, data_root=None, data_list=None, transform=None, mode='train', use_coco=False, use_split_coco=False,
                 args={}):
        assert mode in ['train', 'val', 'test']
        
        self.mode = mode
        self.split = split  
        self.shot = shot
        self.data_root = data_root

        self.meta_aug = args.get('meta_aug', 0)
        self.aug_th = args.get('aug_th', [0.15, 0.30])
        self.aug_type = args.get('aug_type', 0)
        self.im_size = args.train_h if mode == 'train' else args.val_size
        if self.meta_aug > 1:
            print("INFO using data augmentation, meta_aug:{}".format(self.meta_aug))

        if not use_coco:
            self.class_list = list(range(1, 21)) #[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]
            if self.split == 3: 
                self.sub_list = list(range(1, 16)) #[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
                self.sub_val_list = list(range(16, 21)) #[16,17,18,19,20]
            elif self.split == 2:
                self.sub_list = list(range(1, 11)) + list(range(16, 21)) #[1,2,3,4,5,6,7,8,9,10,16,17,18,19,20]
                self.sub_val_list = list(range(11, 16)) #[11,12,13,14,15]
            elif self.split == 1:
                self.sub_list = list(range(1, 6)) + list(range(11, 21)) #[1,2,3,4,5,11,12,13,14,15,16,17,18,19,20]
                self.sub_val_list = list(range(6, 11)) #[6,7,8,9,10]
            elif self.split == 0:
                self.sub_list = list(range(6, 21)) #[6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]
                self.sub_val_list = list(range(1, 6)) #[1,2,3,4,5]

        else:
            if use_split_coco:
                print('INFO: using SPLIT COCO')
                self.class_list = list(range(1, 81))
                if self.split == 3:
                    self.sub_val_list = list(range(4, 81, 4))
                    self.sub_list = list(set(self.class_list) - set(self.sub_val_list))                    
                elif self.split == 2:
                    self.sub_val_list = list(range(3, 80, 4))
                    self.sub_list = list(set(self.class_list) - set(self.sub_val_list))    
                elif self.split == 1:
                    self.sub_val_list = list(range(2, 79, 4))
                    self.sub_list = list(set(self.class_list) - set(self.sub_val_list))    
                elif self.split == 0:
                    self.sub_val_list = list(range(1, 78, 4))
                    self.sub_list = list(set(self.class_list) - set(self.sub_val_list))    
            else:
                print('INFO: using COCO')
                self.class_list = list(range(1, 81))
                if self.split == 3:
                    self.sub_list = list(range(1, 61))
                    self.sub_val_list = list(range(61, 81))
                elif self.split == 2:
                    self.sub_list = list(range(1, 41)) + list(range(61, 81))
                    self.sub_val_list = list(range(41, 61))
                elif self.split == 1:
                    self.sub_list = list(range(1, 21)) + list(range(41, 81))
                    self.sub_val_list = list(range(21, 41))
                elif self.split == 0:
                    self.sub_list = list(range(21, 81)) 
                    self.sub_val_list = list(range(1, 21))    

        print('sub_list: ', self.sub_list)
        print('sub_val_list: ', self.sub_val_list)    

        if self.mode == 'train':
            self.data_list, self.sub_class_file_list = make_dataset(split, data_root, data_list, self.sub_list)
            assert len(self.sub_class_file_list.keys()) == len(self.sub_list)
        elif self.mode == 'val':
            self.data_list, self.sub_class_file_list = make_dataset(split, data_root, data_list, self.sub_val_list)
            assert len(self.sub_class_file_list.keys()) == len(self.sub_val_list) 
        self.transform = transform


    def __len__(self):
        return len(self.data_list)

    def __getitem__(self, index):
        label_class = []
        image_path, label_path = self.data_list[index]   # 用每一张图片 作为 query image
        image = cv2.imread(image_path, cv2.IMREAD_COLOR) 
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)  
        image = np.float32(image)
        label = cv2.imread(label_path, cv2.IMREAD_GRAYSCALE)  

        if image.shape[0] != label.shape[0] or image.shape[1] != label.shape[1]:
            raise (RuntimeError("Query Image & label shape mismatch: " + image_path + " " + label_path + "\n"))          
        label_class = np.unique(label).tolist()
        if 0 in label_class:
            label_class.remove(0)
        if 255 in label_class:
            label_class.remove(255) 
        new_label_class = []       
        for c in label_class:
            if c in self.sub_val_list: # meta test过程中的cls list
                if self.mode == 'val' or self.mode == 'test':
                    new_label_class.append(c)
            if c in self.sub_list:     # meta train中的cls list
                if self.mode == 'train':
                    new_label_class.append(c)
        label_class = new_label_class        # 当前image所有关心的cls
        assert len(label_class) > 0


        # 决定当前任务（segment哪个cls),得到query image的GT label
        class_chosen = label_class[random.randint(1,len(label_class))-1]   ################## 选取target cls, convert label to binary
        class_chosen = class_chosen
        target_pix = np.where(label == class_chosen)
        ignore_pix = np.where(label == 255)
        label[:,:] = 0
        if target_pix[0].shape[0] > 0:
            label[target_pix[0],target_pix[1]] = 1 
        label[ignore_pix[0],ignore_pix[1]] = 255           


        file_class_chosen = self.sub_class_file_list[class_chosen]   # 从中选取啊support image
        num_file = len(file_class_chosen)

        support_image_path_list = []          ########################################## 存所有support image 和 label的路径
        support_label_path_list = []
        support_idx_list = [] # 相对 file_class_chosen的 idx
        for k in range(self.shot):
            support_idx = random.randint(1,num_file)-1
            support_image_path = image_path
            support_label_path = label_path
            while((support_image_path == image_path and support_label_path == label_path) or support_idx in support_idx_list):
                support_idx = random.randint(1,num_file)-1
                support_image_path, support_label_path = file_class_chosen[support_idx]                
            support_idx_list.append(support_idx)
            support_image_path_list.append(support_image_path)
            support_label_path_list.append(support_label_path)

        support_image_list = []        ######################################################## 读取support image & label
        support_label_list = []
        subcls_list = []          # 纪录每张图片所用的cls (有多个cls,只关注一个） index wrt sub_list/sub_val_list
        for k in range(self.shot):  
            if self.mode == 'train':
                subcls_list.append(self.sub_list.index(class_chosen))
            else:
                subcls_list.append(self.sub_val_list.index(class_chosen))
            support_image_path = support_image_path_list[k]
            support_label_path = support_label_path_list[k] 
            support_image = cv2.imread(support_image_path, cv2.IMREAD_COLOR)      
            support_image = cv2.cvtColor(support_image, cv2.COLOR_BGR2RGB)
            support_image = np.float32(support_image)
            support_label = cv2.imread(support_label_path, cv2.IMREAD_GRAYSCALE)
            target_pix = np.where(support_label == class_chosen)
            ignore_pix = np.where(support_label == 255)
            support_label[:,:] = 0
            support_label[target_pix[0],target_pix[1]] = 1 
            support_label[ignore_pix[0],ignore_pix[1]] = 255
            if support_image.shape[0] != support_label.shape[0] or support_image.shape[1] != support_label.shape[1]:
                raise (RuntimeError("Support Image & label shape mismatch: " + support_image_path + " " + support_label_path + "\n"))            
            support_image_list.append(support_image)
            support_label_list.append(support_label)
        assert len(support_label_list) == self.shot and len(support_image_list) == self.shot                    
        
        raw_label = label.copy()   # query image raw label
        if self.transform is not None:
            image, label = self.transform(image, label)
            for k in range(self.shot):
                if self.meta_aug > 1:
                    org_img, org_label = self.transform(support_image_list[k], support_label_list[k])  # flip and resize
                    label_freq = np.bincount(support_label_list[k].flatten())
                    fg_ratio = label_freq[1] / (label_freq[0] + label_freq[1])  # np.sum(label_freq)

                    if self.aug_type == 0:
                        new_img, new_label = self.get_aug_data0(fg_ratio, support_image_list[k], support_label_list[k])
                    elif self.aug_type == 1:
                        new_img, new_label = self.get_aug_data1(fg_ratio, support_image_list[k], support_label_list[k])

                    if new_img is not None:
                        support_image_list[k] = torch.cat([org_img.unsqueeze(0), new_img], dim=0)
                        support_label_list[k] = torch.cat([org_label.unsqueeze(0), new_label], dim=0)
                    else:
                        support_image_list[k], support_label_list[k] = org_img.unsqueeze(0), org_label.unsqueeze(0)

                else:
                    support_image_list[k], support_label_list[k] = self.transform(support_image_list[k], support_label_list[k])
                    support_image_list[k] = support_image_list[k].unsqueeze(0)
                    support_label_list[k] = support_label_list[k].unsqueeze(0)

        s_x = torch.cat(support_image_list, 0)
        s_y = torch.cat(support_label_list, 0)

        if self.mode == 'train':
            return image, label, s_x, s_y, subcls_list
            # image: query image (变换后), label: query label(变换后)，s_x: support img concat, s_y:support label concat,
            # sub_cls_list, 每个support image所关注的label 对应的index
        else:
            return image, label, s_x, s_y, subcls_list, raw_label

    def get_aug_data0(self, fg_ratio, support_image, support_label):  # only size augmentation, no color augmentation
        if fg_ratio <= self.aug_th[0] or fg_ratio >= self.aug_th[1]:
            if fg_ratio <= self.aug_th[0]:
                k = 2 if fg_ratio <= 0.03 else 3  # whether to crop at 1/2 or 1/3
                meta_trans = Compose([FitCrop(k=k)] + self.transform.segtransform[-3:])
            else:
                scale = self.im_size / max(support_label.shape) * (0.7 if fg_ratio > 0.3 else 0.8)
                meta_trans = Compose([RandScale(scale=(scale, scale + 0.05), fixed_size=self.im_size, padding=[0, 0, 0])] + self.transform.segtransform[-2:])
            new_img, new_label = meta_trans(support_image, support_label)
            return new_img.unsqueeze(0), new_label.unsqueeze(0)
        else:
            new_img, new_label = self.transform(support_image, support_label)
            return new_img.unsqueeze(0), new_label.unsqueeze(0)

    def get_aug_data1(self, fg_ratio, support_image, support_label):   # only size augmentation, no color augmentation
        if fg_ratio <= self.aug_th[0] or fg_ratio >= self.aug_th[1]:
            if fg_ratio <= self.aug_th[0]:
                k = 2 if fg_ratio <= 0.03 else 3  # whether to crop at 1/2 or 1/3
                meta_trans = Compose([FitCrop(k=k)] + self.transform.segtransform[-3:])
            else:
                scale = self.im_size / max(support_label.shape) * (0.7 if fg_ratio > 0.3 else 0.8)
                meta_trans = Compose([RandScale(scale=(scale, scale + 0.05), fixed_size=self.im_size, padding=[0,0,0])] + self.transform.segtransform[-2:])
            new_img, new_label = meta_trans(support_image, support_label)
            return new_img.unsqueeze(0), new_label.unsqueeze(0)
        else:
            return None, None
