import os
import pickle
from tqdm import tqdm
from datetime import datetime

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.utils.data.dataset import Subset

import math
import warnings

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=FutureWarning)
    from torch.utils.tensorboard import SummaryWriter

from config import ex
from data.util import get_dataset, IdxDataset, ZippedDataset
from module.loss import GeneralizedCELoss
from module.util import get_model
from util import MultiDimAverageMeter, EMA

# BiasAdv
def pgd_attack_adv(device, model_b, model_d, images, labels, eps=0.4, alpha=4/255, lmd = 2, iters=40) :
    images = images.to(device)
    labels = labels.to(device)

    loss = nn.CrossEntropyLoss(reduction='none')
        
    ori_images = images.data
        
    for i in range(iters) :    
        images.requires_grad = True
        outputs_b = model_b(images)
        outputs_d = model_d(images)
        # outputs1 = model(images)
        # output2
        model_b.zero_grad()
        model_d.zero_grad()
        
        # print('###shape output###: ', outputs_b.shape)
        # print('###shape labels###: ', labels.shape)
        cost_b = loss(outputs_b, labels).to(device)
        

        cost_d = loss(outputs_d, labels).to(device)

        cost = (cost_b - lmd * cost_d).mean()
        
        
        cost.backward()
        # cost 1
        # cost 2
        # cost1-lambda*cost2
        

        adv_images = images + alpha*images.grad.sign()
        
        
        eta = torch.clamp(adv_images - ori_images, min=-eps, max=eps)
        images = torch.clamp(ori_images + eta, min=0, max=1).detach_()
            
    
    mode = 3
    
    if mode == 0:
        print('###################')
        print('label: ', labels)
        print('ori predicted by biased model: ', model_b(ori_images))
        print('ori predicted by debiased model: ', model_d(ori_images))
        print('adv predicted by biased model: ', model_b(images))
        print('adv predicted by debiased model: ', model_d(images))
    elif mode == 1:
        print('###################')
        print('label: ', labels)
        print('ori predicted by biased model: ', torch.argmax(model_b(ori_images), dim=1))
        print('ori predicted by debiased model: ', torch.argmax(model_d(ori_images), dim=1))
        print('adv predicted by biased model: ', torch.argmax(model_b(images), dim=1))
        print('adv predicted by debiased model: ', torch.argmax(model_d(images), dim=1))
    else:
        return images
    
        
    return images


@ex.automain
def train(
    main_tag,
    dataset_tag,
    model_tag,
    data_dir,
    log_dir,
    device,
    target_attr_idx,
    bias_attr_idx,
    main_num_steps,
    main_valid_freq,
    main_batch_size,
    main_optimizer_tag,
    main_learning_rate,
    main_weight_decay,
):

    print(dataset_tag) #ColoredMNIST
    
    device = torch.device(device)
    start_time = datetime.now()
    writer = SummaryWriter(os.path.join(log_dir, "summary", main_tag))
    train_dataset = get_dataset(
        dataset_tag,
        data_dir=data_dir,
        dataset_split="train",
        transform_split="train",
    )
    valid_dataset = get_dataset(
        dataset_tag,
        data_dir=data_dir,
        dataset_split="eval",
        transform_split="eval",
    )

    train_target_attr = train_dataset.attr[:, target_attr_idx]
    train_bias_attr = train_dataset.attr[:, bias_attr_idx]

    print('train_target_attr shape: ', train_target_attr.shape)
    print('train_bias_attr shape: ', train_bias_attr.shape)
    
    attr_dims = []
    attr_dims.append(torch.max(train_target_attr).item() + 1)
    attr_dims.append(torch.max(train_bias_attr).item() + 1)
    num_classes = attr_dims[0]
        
    train_dataset = IdxDataset(train_dataset)
    valid_dataset = IdxDataset(valid_dataset)    

    # make loader    
    train_loader = DataLoader(
        train_dataset,
        batch_size=main_batch_size,
        shuffle=True,
        num_workers=16,
        pin_memory=True,
    )

    valid_loader = DataLoader(
        valid_dataset,
        batch_size=1000,
        shuffle=True,
        num_workers=16,
        pin_memory=True,
    )
    
    # define model and optimizer
    model_b = get_model(model_tag, attr_dims[0]).to(device)
    model_d = get_model(model_tag, attr_dims[0]).to(device)
    
    if main_optimizer_tag == "SGD":
        optimizer_b = torch.optim.SGD(
            model_b.parameters(),
            lr=main_learning_rate,
            weight_decay=main_weight_decay,
            momentum=0.9,
        )
        optimizer_d = torch.optim.SGD(
            model_d.parameters(),
            lr=main_learning_rate,
            weight_decay=main_weight_decay,
            momentum=0.9,
        )
    elif main_optimizer_tag == "Adam":
        optimizer_b = torch.optim.Adam(
            model_b.parameters(),
            lr=main_learning_rate,
            weight_decay=main_weight_decay,
        )
        optimizer_d = torch.optim.Adam(
            model_d.parameters(),
            lr=main_learning_rate,
            weight_decay=main_weight_decay,
        )
    elif main_optimizer_tag == "AdamW":
        optimizer_b = torch.optim.AdamW(
            model_b.parameters(),
            lr=main_learning_rate,
            weight_decay=main_weight_decay,
        )
        optimizer_d = torch.optim.AdamW(
            model_d.parameters(),
            lr=main_learning_rate,
            weight_decay=main_weight_decay,
        )
    else:
        raise NotImplementedError
    
    # define loss
    criterion = nn.CrossEntropyLoss(reduction='none')
    bias_criterion = GeneralizedCELoss() #hacked
    
    sample_loss_ema_b = EMA(torch.LongTensor(train_target_attr), alpha=0.7)
    sample_loss_ema_d = EMA(torch.LongTensor(train_target_attr), alpha=0.7)

    # define evaluation function
    def evaluate(model, data_loader):
        model.eval()
        acc = 0
        attrwise_acc_meter = MultiDimAverageMeter(attr_dims)
        for index, data, attr in tqdm(data_loader, leave=False):
            label = attr[:, target_attr_idx]
            data = data.to(device)
            attr = attr.to(device)
            label = label.to(device)
            with torch.no_grad():
                logit = model(data)
                pred = logit.data.max(1, keepdim=True)[1].squeeze(1)
                correct = (pred == label).long()

            attr = attr[:, [target_attr_idx, bias_attr_idx]]

            attrwise_acc_meter.add(correct.cpu(), attr.cpu())

        accs = attrwise_acc_meter.get_mean()

        model.train()

        return accs
    
    def disparate_impact_helper(digit, model, data_loader):
    #
    # disparate impact = ((num_correct_digit1(color=red))/  (num_digit1(color=red)))/((num_correct_digit1(color!=red))/  (num_digit1(color!=red)))
        model.eval()
        result = []
        for index, data, attr in tqdm(data_loader, leave=False):
            label = attr[:, target_attr_idx]
            data = data.to(device)
            attr = attr.to(device)
            label = label.to(device)
            with torch.no_grad():
                logit = model(data)
                pred = logit.data.max(1, keepdim=True)[1].squeeze(1)
                unpriviledge_count = torch.sum((attr[:, 0] == digit) & (attr[:, 1] == digit)) # 1,1
                priviledge_count = torch.sum((attr[:, 0] == digit) & (attr[:, 1] != digit))   # 1,2
                
                unpriviledge_correct_count = torch.sum((pred == digit) & (attr[:, 0] == digit) & (attr[:, 1] == digit))
                priviledge_correct_count = torch.sum((pred == digit) & (attr[:, 0] == digit) & (attr[:, 1] != digit))

                disparate_impact = ((unpriviledge_correct_count / unpriviledge_count) / (priviledge_correct_count / priviledge_count)).item()

                if not (math.isnan(disparate_impact) or math.isinf(disparate_impact)):
                    result.append(disparate_impact)


        
        disparate_impact = np.mean(np.array(result))

        model.train()

        # print('disparate_impact on ' + str(digit) +': ', disparate_impact)

        return disparate_impact
    
    # jointly training biased/de-biased model
    valid_attrwise_accs_list = []
    num_updated = 0
    
    for step in tqdm(range(main_num_steps)):
        
        # train main model
        try:
            index, data, attr = next(train_iter)
        except:
            train_iter = iter(train_loader)
            index, data, attr = next(train_iter)

        #original version
        data = data.to(device)
        attr = attr.to(device)
        label = attr[:, target_attr_idx]
        
        
        #hacked verison
        data_adv = pgd_attack_adv(device, model_b, model_d, data, label)
        
        if step % 10 == 0:
            writer.add_image('adv images', data_adv[0], step)
            writer.add_image('ori images', data[0], step)
        
        # data_adv = data
        # data_adv = data_adv.to(device)
        
        target_label = attr[:, target_attr_idx]
        bias_label = attr[:, bias_attr_idx]
        
        logit_b = model_b(data)
        if np.isnan(logit_b.mean().item()):
            print(logit_b)
            raise NameError('logit_b')
        logit_d = model_d(data)
        logit_d_adv = model_d(data_adv) 
        #hacked end
        
        
#         print('###shape output###: ', logit_b.shape)
#         print('###shape labels###: ', label.shape)
        
        loss_b = criterion(logit_b, label).cpu().detach()
        loss_d = criterion(logit_d, label).cpu().detach()

        #hacked
        loss_d_adv = criterion(logit_d_adv, label).cpu().detach()
                

        if np.isnan(loss_b.mean().item()):
            raise NameError('loss_b')
        if np.isnan(loss_d.mean().item()):
            raise NameError('loss_d')
        if np.isnan(loss_d_adv.mean().item()):
            raise NameError('loss_d_adv')
        
        loss_per_sample_b = loss_b
        loss_per_sample_d = loss_d

        
        # EMA sample loss
        sample_loss_ema_b.update(loss_b, index)
        sample_loss_ema_d.update(loss_d, index)
        
        # class-wise normalize
        loss_b = sample_loss_ema_b.parameter[index].clone().detach()
        loss_d = sample_loss_ema_d.parameter[index].clone().detach()
        
        if np.isnan(loss_b.mean().item()):
            raise NameError('loss_b_ema')
        if np.isnan(loss_d.mean().item()):
            raise NameError('loss_d_ema')
        
        label_cpu = label.cpu()
        
        for c in range(num_classes):
            class_index = np.where(label_cpu == c)[0]
            max_loss_b = sample_loss_ema_b.max_loss(c)
            max_loss_d = sample_loss_ema_d.max_loss(c)
            loss_b[class_index] /= max_loss_b
            loss_d[class_index] /= max_loss_d
            
        # re-weighting based on loss value / generalized CE for biased model
        loss_weight = loss_b / (loss_b + loss_d + 1e-8)

        #hacked
        beta = 0.4
        loss_weight_adv = beta * (1 - loss_weight)
        
        if np.isnan(loss_weight.mean().item()):
            raise NameError('loss_weight')
            
        loss_b_update = bias_criterion(logit_b, label)

        if np.isnan(loss_b_update.mean().item()):
            raise NameError('loss_b_update')
        # loss_d_update = criterion(logit_d, label) * loss_weight.to(device)
        loss_d_update = criterion(logit_d, label) * loss_weight.to(device) + criterion(logit_d_adv, label) * loss_weight_adv.to(device) #hacked version

        if np.isnan(loss_d_update.mean().item()):
            raise NameError('loss_d_update')
        loss = loss_b_update.mean() + loss_d_update.mean()
        
        num_updated += loss_weight.mean().item() * data.size(0)

        optimizer_b.zero_grad()
        optimizer_d.zero_grad()
        
        loss.backward() 
        optimizer_b.step() #line 10


        optimizer_d.step()
        
        main_log_freq = 10
        if step % main_log_freq == 0:
        
            writer.add_scalar("loss/b_train", loss_per_sample_b.mean(), step)
            writer.add_scalar("loss/d_train", loss_per_sample_d.mean(), step)

            bias_attr = attr[:, bias_attr_idx]

            aligned_mask = (label == bias_attr)
            skewed_mask = (label != bias_attr)
            

            if aligned_mask.any().item():
                writer.add_scalar("loss/b_train_aligned", loss_per_sample_b[aligned_mask].mean(), step)
                writer.add_scalar("loss/d_train_aligned", loss_per_sample_d[aligned_mask].mean(), step)
                writer.add_scalar('loss_weight/aligned', loss_weight[aligned_mask].mean(), step)

            if skewed_mask.any().item():
                writer.add_scalar("loss/b_train_skewed", loss_per_sample_b[skewed_mask].mean(), step)
                writer.add_scalar("loss/d_train_skewed", loss_per_sample_d[skewed_mask].mean(), step)
                writer.add_scalar('loss_weight/skewed', loss_weight[skewed_mask].mean(), step)

        if step % main_valid_freq == 0:
            valid_attrwise_accs_b = evaluate(model_b, valid_loader)
            valid_attrwise_accs_d = evaluate(model_d, valid_loader)
            valid_attrwise_accs_list.append(valid_attrwise_accs_d)
            valid_accs_b = torch.mean(valid_attrwise_accs_b)
            writer.add_scalar("acc/b_valid", valid_accs_b, step)
            valid_accs_d = torch.mean(valid_attrwise_accs_d)
            writer.add_scalar("acc/d_valid", valid_accs_d, step)


            #disparate impact
            disparate_impact_b_arr = []
            disparate_impact_d_arr = []
            for i in range(10):
                disparate_impact_b = disparate_impact_helper(i, model_b, valid_loader)
                disparate_impact_d = disparate_impact_helper(i, model_d, valid_loader)

                disparate_impact_b_arr.append(disparate_impact_b)
                disparate_impact_d_arr.append(disparate_impact_d)

                writer.add_scalar('disparate_impact_b/' + str(i), disparate_impact_b, step)   
                writer.add_scalar('disparate_impact_d/' + str(i), disparate_impact_d, step)

            # print('mean_b: ', np.mean(np.array(disparate_impact_b_arr)))
            # print('mean_d: ', np.mean(np.array(disparate_impact_d_arr)))

            writer.add_scalar('disparate_impact_b_mean/', np.mean(np.array(disparate_impact_b_arr)), step)   
            writer.add_scalar('disparate_impact_d_mean/', np.mean(np.array(disparate_impact_d_arr)), step)


            


            eye_tsr = torch.eye(attr_dims[0]).long()
            
            writer.add_scalar(
                "acc/b_valid_aligned",
                valid_attrwise_accs_b[eye_tsr == 1].mean(),
                step,
            )
            writer.add_scalar(
                "acc/b_valid_skewed",
                valid_attrwise_accs_b[eye_tsr == 0].mean(),
                step,
            )
            writer.add_scalar(
                "acc/d_valid_aligned",
                valid_attrwise_accs_d[eye_tsr == 1].mean(),
                step,
            )
            writer.add_scalar(
                "acc/d_valid_skewed",
                valid_attrwise_accs_d[eye_tsr == 0].mean(),
                step,
            )
            
            num_updated_avg = num_updated / main_batch_size / main_valid_freq
            writer.add_scalar("num_updated/all", num_updated_avg, step)
            num_updated = 0

    os.makedirs(os.path.join(log_dir, "result", main_tag), exist_ok=True)
    result_path = os.path.join(log_dir, "result", main_tag, "result.th")
    model_path = os.path.join(log_dir, "result", main_tag, "model.th")
    valid_attrwise_accs_list = torch.stack(valid_attrwise_accs_list)
    with open(result_path, "wb") as f:
        torch.save({"valid/attrwise_accs": valid_attrwise_accs_list}, f)
    state_dict = {
        'steps': step, 
        'state_dict': model_d.state_dict(), 
        'optimizer': optimizer_d.state_dict(), 
    }
    with open(model_path, "wb") as f:
        torch.save(state_dict, f)
    


