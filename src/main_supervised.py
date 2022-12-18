import argparse
import os

import monai
import numpy as np
import torch
import yaml
from data_utils.prepare_dataset import prepare_dataset_supervised
from tqdm import tqdm
from utils import AttributeHashmap, EarlyStopping, log, parse_settings, seed_everything
from utils.metrics import dice_coeff


def save_weights(model_save_path: str, model):
    os.makedirs(os.path.dirname(model_save_path), exist_ok=True)
    torch.save(model.state_dict(), model_save_path)
    return


def load_weights(model_save_path: str, model):
    model.load_state_dict(torch.load(model_save_path))
    return


def save_numpy(config: AttributeHashmap, image_batch: torch.Tensor,
               label_true_batch: torch.Tensor, label_pred_batch: torch.Tensor):
    image_batch = image_batch.cpu().detach().numpy()
    label_true_batch = label_true_batch.cpu().detach().numpy()
    label_pred_batch = label_pred_batch.cpu().detach().numpy()
    # channel-first to channel-last
    image_batch = np.moveaxis(image_batch, 1, -1)

    B = image_batch.shape[0]

    # Save the images, labels, and latent embeddings as numpy files for future reference.
    save_path_numpy = '%s/%s/' % (config.output_save_path, 'numpy_files')
    os.makedirs(save_path_numpy, exist_ok=True)
    for image_idx in tqdm(range(B)):
        with open(
                '%s/%s' %
            (save_path_numpy, 'sample_%s.npz' % str(image_idx).zfill(5)),
                'wb+') as f:
            np.savez(f,
                     image=image_batch[image_idx, ...],
                     label_true=label_true_batch[image_idx, ...],
                     label_pred=label_pred_batch[image_idx, ...])
    return


def train(config: AttributeHashmap):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    train_set, val_set, test_set, num_image_channel = \
        prepare_dataset_supervised(config=config)

    # Build the model
    if config.supervised_network == 'unet':
        model = torch.hub.load('mateuszbuda/brain-segmentation-pytorch',
                               'unet',
                               in_channels=num_image_channel,
                               out_channels=1,
                               init_features=config.num_kernels,
                               pretrained=config.pretrained).to(device)
    elif config.supervised_network == 'nnunet':
        model = torch.nn.Sequential(
            monai.networks.nets.DynUNet(spatial_dims=2,
                                        in_channels=num_image_channel,
                                        out_channels=1,
                                        kernel_size=[5, 5, 5, 5],
                                        filters=[16, 32, 64, 128],
                                        strides=[1, 1, 1, 1],
                                        upsample_kernel_size=[1, 1, 1, 1]),
            torch.nn.Sigmoid()
        ).to(device)
    else:
        raise ValueError(
            '`main_supervised.py: config.supervised_network = %s is not supported.'
            % config.supervised_network)
    optimizer = torch.optim.AdamW(model.parameters(),
                                  lr=config.learning_rate,
                                  weight_decay=config.weight_decay)
    early_stopper = EarlyStopping(mode='min',
                                  patience=config.patience,
                                  percentage=False)

    loss_fn_supervised = torch.nn.BCELoss()

    best_val_loss = np.inf

    for epoch_idx in tqdm(range(config.max_epochs)):
        train_loss, train_dice = 0, []

        model.train()
        for _, (x_train, seg_true) in enumerate(train_set):
            x_train = x_train.type(torch.FloatTensor).to(device)
            seg_true = seg_true.type(torch.FloatTensor).to(device)
            seg_pred = model(x_train).squeeze(1)
            seg_pred_binary = (seg_pred > 0.5).type(
                torch.FloatTensor).to(device)

            loss = loss_fn_supervised(seg_pred, seg_true)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            train_loss += loss.item()
            for batch_idx in range(seg_true.shape[0]):
                train_dice.append(
                    dice_coeff(
                        seg_pred_binary[batch_idx, ...].cpu().detach().numpy(),
                        seg_true[batch_idx, ...].cpu().detach().numpy()))

        train_loss = train_loss / len(train_set)

        log('Train [%s/%s] loss: %.3f, dice: %.3f \u00B1 %.3f' %
            (epoch_idx + 1, config.max_epochs, train_loss, np.mean(train_dice),
             np.std(train_dice) / np.sqrt(len(train_dice))),
            filepath=config.log_dir,
            to_console=False)

        val_loss, val_dice = 0, []
        model.eval()
        with torch.no_grad():
            for _, (x_val, seg_true) in enumerate(val_set):
                x_val = x_val.type(torch.FloatTensor).to(device)
                seg_true = seg_true.type(torch.FloatTensor).to(device)
                seg_pred = model(x_val).squeeze(1)
                seg_pred_binary = (seg_pred > 0.5).type(
                    torch.FloatTensor).to(device)

                loss = loss_fn_supervised(seg_pred, seg_true)

                val_loss += loss.item()
                for batch_idx in range(seg_true.shape[0]):
                    val_dice.append(
                        dice_coeff(
                            seg_pred_binary[batch_idx,
                                            ...].cpu().detach().numpy(),
                            seg_true[batch_idx, ...].cpu().detach().numpy()))

        val_loss = val_loss / len(val_set)

        log('Validation [%s/%s] loss: %.3f, dice: %.3f \u00B1 %.3f' %
            (epoch_idx + 1, config.max_epochs, val_loss, np.mean(val_dice),
             np.std(val_dice) / np.sqrt(len(val_dice))),
            filepath=config.log_dir,
            to_console=False)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            save_weights(config.model_save_path, model)
            log('%s: Model weights successfully saved.' %
                config.supervised_network,
                filepath=config.log_dir,
                to_console=False)

        if early_stopper.step(val_loss):
            # If the validation loss stop decreasing, stop training.
            log('Early stopping criterion met. Ending training.',
                filepath=config.log_dir,
                to_console=True)
            break
    return


def test(config: AttributeHashmap):
    device = torch.device('cpu')
    train_set, val_set, test_set, num_image_channel = \
        prepare_dataset_supervised(config=config)

    # Build the model
    if config.supervised_network == 'unet':
        model = torch.hub.load('mateuszbuda/brain-segmentation-pytorch',
                               'unet',
                               in_channels=num_image_channel,
                               out_channels=1,
                               init_features=config.num_kernels,
                               pretrained=config.pretrained).to(device)
    elif config.supervised_network == 'nnunet':
        model = torch.nn.Sequential(
            monai.networks.nets.DynUNet(spatial_dims=2,
                                        in_channels=num_image_channel,
                                        out_channels=1,
                                        kernel_size=[5, 5, 5, 5],
                                        filters=[16, 32, 64, 128],
                                        strides=[1, 1, 1, 1],
                                        upsample_kernel_size=[1, 1, 1, 1]),
            torch.nn.Sigmoid()
        ).to(device)
    else:
        raise ValueError(
            '`main_supervised.py: config.supervised_network = %s is not supported.'
            % config.supervised_network)
    load_weights(config.model_save_path, model)
    log('%s: Model weights successfully loaded.' % config.supervised_network,
        to_console=True)

    loss_fn_supervised = torch.nn.CrossEntropyLoss()

    test_loss, test_dice = 0, []
    model.eval()

    with torch.no_grad():
        for _, (x_test, seg_true) in enumerate(test_set):
            x_test = x_test.type(torch.FloatTensor).to(device)
            seg_true = seg_true.type(torch.FloatTensor).to(device)
            seg_pred = model(x_test).squeeze(1)
            seg_pred_binary = (seg_pred > 0.5).type(
                torch.FloatTensor).to(device)

            loss = loss_fn_supervised(seg_true, seg_pred)
            for batch_idx in range(seg_true.shape[0]):
                test_dice.append(
                    dice_coeff(
                        seg_pred_binary[batch_idx, ...].cpu().detach().numpy(),
                        seg_true[batch_idx, ...].cpu().detach().numpy()))

            test_loss += loss.item()

            save_numpy(config=config,
                       image_batch=x_test,
                       label_true_batch=seg_true,
                       label_pred_batch=seg_pred)

    test_loss = test_loss / len(test_set)

    log('Test loss: %.3f, dice: %.3f \u00B1 %.3f.' %
        (test_loss, np.mean(test_dice),
         np.std(test_dice) / np.sqrt(len(test_dice))),
        filepath=config.log_dir,
        to_console=True)
    return


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Entry point to run CUTS.')
    parser.add_argument('--mode', help='`train` or `test`?', required=True)
    parser.add_argument('--config',
                        help='Path to config yaml file.',
                        required=True)
    args = vars(parser.parse_args())

    args = AttributeHashmap(args)
    config = AttributeHashmap(yaml.safe_load(open(args.config)))
    config.config_file_name = args.config
    config = parse_settings(config, log_settings=args.mode == 'train')

    # Currently supports 2 modes: `train`: Train+Validation+Test & `test`: Test.
    assert args.mode in ['train', 'test']

    seed_everything(config.random_seed)

    if args.mode == 'train':
        train(config=config)
        test(config=config)
    elif args.mode == 'test':
        test(config=config)
