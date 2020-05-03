import os
import random
import time
from typing import Tuple

import matplotlib.pyplot as plt
import torch
from torch import nn

from data import LABELED_DATASETS, LabeledSubdataset
from models.images.classification.backbones import NoFlatteningBackbone
from models.images.classification.few_shot_learning import evaluate_solution_episodes, accuracy, FSLEpisodeSampler, \
    FEATURE_EXTRACTORS, FSLEpisodeSamplerGlobalLabels, OPTIMIZERS
from sessions import Session
from utils import pretty_time, remove_dim
from visualization.plots import PlotterWindow

MAX_BATCH_SIZE = 20000

EPOCHS_MULTIPLIER = 2


def decoder_block(input_dim: int, output_dim: int, padding=1, output_padding=1):
    return nn.Sequential(*[
        nn.ConvTranspose2d(input_dim, output_dim, kernel_size=3, stride=2, padding=padding,
                           output_padding=output_padding),
        nn.LeakyReLU(),
        nn.BatchNorm2d(output_dim),
    ])


class ConvNetDecoder(nn.Module):
    def __init__(self, input_dim=64, input_map_size=1):
        super(ConvNetDecoder, self).__init__()
        self.input_dim = input_dim
        self.input_map_size = input_map_size
        self.map_size = 6

        self.linear = nn.Linear(in_features=input_dim * (input_map_size ** 2),
                                out_features=input_dim * (self.map_size ** 2))
        self.relu = nn.ReLU()

        self.layer1 = decoder_block(input_dim, input_dim, output_padding=0)
        self.layer2 = decoder_block(input_dim, input_dim, output_padding=0)
        self.layer3 = decoder_block(input_dim, input_dim)
        self.layer4 = decoder_block(input_dim, 3)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.view(x.size(0), -1)
        x = self.linear(x)
        x = self.relu(x)
        x = x.view(x.size(0), self.input_dim, self.map_size, self.map_size)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        return x


class ProtoNet_AE(nn.Module):
    def __init__(self, backbone: NoFlatteningBackbone):
        super(ProtoNet_AE, self).__init__()
        self.feature_extractor = backbone
        self.decoder = ConvNetDecoder(input_dim=backbone.output_features(),
                                      input_map_size=backbone.output_featmap_size())

        self.latent_features = backbone.output_features()
        self.latent_featmap_size = backbone.output_featmap_size()

        self.loss_fn = nn.CrossEntropyLoss()
        self.loss_ae_fn = nn.MSELoss()

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, a=0, mode='fan_in', nonlinearity='conv2d')
                try:
                    nn.init.constant_(m.bias, 0)
                except AttributeError as e:
                    pass

            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def extract_features(self, batch: torch.Tensor) -> torch.Tensor:
        minibatches = batch.split(split_size=MAX_BATCH_SIZE)
        xs = []
        for minibatch in minibatches:
            output = self.feature_extractor(minibatch)
            output = output.view(output.size(0), -1)
            xs.append(output)
        x = torch.cat(xs)
        return x

    def get_prototypes(self, support_set: torch.Tensor):
        return torch.mean(support_set, dim=1)

    def forward(self, support_set: torch.Tensor, query_set: torch.Tensor) -> torch.Tensor:
        n_classes = support_set.size(0)
        support_set_size = support_set.size(1)
        query_set_size = query_set.size(0)

        support_set_features = self.extract_features(remove_dim(support_set, 1)).view(n_classes,
                                                                                      support_set_size, -1)

        query_set_features = self.extract_features(query_set)

        class_prototypes = self.get_prototypes(support_set_features)

        query_set_features_prepared = query_set_features.unsqueeze(1).repeat_interleave(repeats=n_classes,
                                                                                        dim=1)

        distance = torch.sum((class_prototypes.unsqueeze(0).repeat_interleave(repeats=query_set_size,
                                                                              dim=0) -
                              query_set_features_prepared).pow(2), dim=2)

        return -distance

    def forward_with_loss(self, support_set: torch.Tensor, query_set: torch.Tensor, labels: torch.Tensor) -> Tuple[
        torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        output = self(support_set, query_set)
        loss_i = self.loss_fn(output, labels) * 0.2

        # Autoencoder loss

        encoded = self.extract_features(query_set).view(-1, self.latent_features, self.latent_featmap_size,
                                                        self.latent_featmap_size)
        decoded = self.decoder(encoded)
        loss_ae = self.loss_ae_fn(decoded.view(decoded.size(0), -1), query_set.view(query_set.size(0), -1))

        loss = loss_i + loss_ae
        return output, loss, loss_i, loss_ae


def train_protonetae(base_subdataset: LabeledSubdataset, val_subdataset: LabeledSubdataset, n_shot: int, n_way: int,
                     n_iterations: int, batch_size: int, eval_period: int,
                     val_batch_size: int,
                     image_size: int,
                     balanced_batches: bool,
                     train_n_way=15,
                     backbone_name='resnet12-np-o',
                     device=torch.device("cuda:0" if torch.cuda.is_available() else "cpu"), **kwargs):
    session_info = {
        "task": "few-shot learning",
        "model": "ProtoNetAE",
        "feature_extractor": backbone_name,
        "n_iterations": n_iterations,
        "eval_period": eval_period,
        # "dataset": dataset_name,
        # "optimizer": optimizer_name,
        "batch_size": batch_size,
        "val_batch_size": val_batch_size,
        "n_shot": n_shot,
        "n_way": n_way,
        "train_n_way": train_n_way,
        "optimizer": 'adam',
        "image_size": image_size,
        "balanced_batches": balanced_batches,
    }

    session_info.update(kwargs)

    backbone = FEATURE_EXTRACTORS[backbone_name]()
    model = ProtoNet_AE(backbone=backbone).to(device)

    optimizer = OPTIMIZERS['adam'](model=model)

    base_sampler = FSLEpisodeSamplerGlobalLabels(subdataset=base_subdataset, n_way=train_n_way, n_shot=n_shot,
                                                 batch_size=batch_size, balanced=balanced_batches)
    val_sampler = FSLEpisodeSampler(subdataset=val_subdataset, n_way=n_way, n_shot=n_shot, batch_size=val_batch_size,
                                    balanced=balanced_batches)

    loss_plotter = PlotterWindow(interval=1000)
    accuracy_plotter = PlotterWindow(interval=1000)

    loss_plotter.new_line('Loss')
    loss_plotter.new_line('Loss Instance')
    loss_plotter.new_line('Loss Autoencoder')
    accuracy_plotter.new_line('Train Accuracy')
    accuracy_plotter.new_line('Validation Accuracy')

    losses = []
    losses_i = []
    losses_ae = []
    acc_train = []
    acc_val = []
    val_iters = []

    best_accuracy = 0
    best_iteration = -1

    print("Training started for parameters:")
    print(session_info)
    print()

    start_time = time.time()

    for iteration in range(n_iterations):
        model.train()

        support_set, batch, global_classes_mapping = base_sampler.sample()
        # print(support_set.size())
        query_set, query_labels = batch
        # print(query_set.size())
        # print(global_classes_mapping)
        query_set = query_set.to(device)
        query_labels = query_labels.to(device)

        optimizer.zero_grad()
        output, loss, loss_i, loss_ae = model.forward_with_loss(support_set, query_set, query_labels)
        loss.backward()
        optimizer.step()

        labels_pred = output.argmax(dim=1)
        labels = query_labels
        cur_accuracy = accuracy(labels=labels, labels_pred=labels_pred)

        loss_plotter.add_point('Loss', iteration, loss.item())
        loss_plotter.add_point('Loss Instance', iteration, loss_i.item())
        loss_plotter.add_point('Loss Autoencoder', iteration, loss_ae.item())
        accuracy_plotter.add_point('Train Accuracy', iteration, cur_accuracy)

        losses.append(loss.item())
        losses_i.append(loss_i.item())
        losses_ae.append(loss_ae.item())
        acc_train.append(cur_accuracy)

        if iteration % eval_period == 0 or iteration == n_iterations - 1:
            val_start_time = time.time()

            val_accuracy = evaluate_solution_episodes(model, val_sampler)
            accuracy_plotter.add_point('Validation Accuracy', iteration, val_accuracy)

            acc_val.append(val_accuracy)
            val_iters.append(iteration + 1)

            if val_accuracy > best_accuracy:
                best_accuracy = val_accuracy
                best_iteration = iteration
                print("Best evaluation result yet!")

            cur_time = time.time()

            val_time = cur_time - val_start_time
            time_used = cur_time - start_time
            time_per_iteration = time_used / (iteration + 1)

            print()
            print("[%d/%d] = %.2f%%\t\tLoss: %.4f" % (
                iteration + 1, n_iterations, (iteration + 1) / n_iterations * 100, loss.item()))
            print("Current validation time: %s" % pretty_time(val_time))

            print('Average iteration time: %s\tEstimated execution time: %s' % (
                pretty_time(time_per_iteration),
                pretty_time(time_per_iteration * (n_iterations - iteration - 1)),
            ))
            print()

    cur_time = time.time()
    training_time = cur_time - start_time
    print("Training finished. Total execution time: %s" % pretty_time(training_time))
    print("Best accuracy is: %.3f" % best_accuracy)
    print("Best iteration is: [%d/%d]" % (best_iteration + 1, n_iterations))
    print()

    session_info['accuracy'] = best_accuracy
    session_info['best_iteration'] = best_iteration
    session_info['execution_time'] = training_time

    session = Session()
    session.build(name="ProtoNetAE", comment=r"ProtoNet + AE Loss Few-Shot Learning",
                  **session_info)
    torch.save(model, os.path.join(session.data['output_dir'], "trained_model_state_dict.tar"))
    iters = list(range(1, n_iterations + 1))

    plt.figure(figsize=(20, 20))
    plt.plot(iters, losses, label="Loss")
    plt.plot(iters, losses_i, label="Loss Instance")
    plt.plot(iters, losses_ae, label="Loss Autoencoder")
    plt.legend()
    plt.savefig(os.path.join(session.data['output_dir'], "loss_plot.png"))

    plt.figure(figsize=(20, 20))
    plt.plot(iters, acc_train, label="Train Accuracy")
    plt.plot(val_iters, acc_val, label="Test Accuracy")
    plt.legend()
    plt.savefig(os.path.join(session.data['output_dir'], "acc_plot.png"))

    session.save_info()


if __name__ == '__main__':
    torch.random.manual_seed(2002)
    random.seed(2002)

    DATASET_NAME = 'miniImageNet'
    BASE_CLASSES = 80
    AUGMENT_PROB = 1.0
    ITERATIONS = 40000 * EPOCHS_MULTIPLIER
    N_WAY = 5
    EVAL_PERIOD = 1000
    RECORD = 200
    IMAGE_SIZE = 84
    BACKBONE = 'conv64-p-o'
    # BACKBONE = 'resnet18'
    BATCH_SIZE = 8 // EPOCHS_MULTIPLIER
    VAL_BATCH_SIZE = 15 // EPOCHS_MULTIPLIER
    BALANCED_BATCHES = True

    # N_SHOT = 5

    print("Preparations for training...")
    dataset = LABELED_DATASETS[DATASET_NAME](augment_prob=AUGMENT_PROB, image_size=IMAGE_SIZE)
    base_subdataset, val_subdataset = dataset.subdataset.extract_classes(BASE_CLASSES)
    base_subdataset.set_test(False)
    val_subdataset.set_test(True)

    for N_SHOT in (1, 5,):
        train_protonetae(base_subdataset=base_subdataset, val_subdataset=val_subdataset, n_shot=N_SHOT, n_way=N_WAY,
                         n_iterations=ITERATIONS, batch_size=BATCH_SIZE,
                         eval_period=EVAL_PERIOD,
                         record=RECORD,
                         augment=AUGMENT_PROB,
                         dataset=DATASET_NAME,
                         base_classes=BASE_CLASSES,
                         dataset_classes=dataset.CLASSES,
                         image_size=IMAGE_SIZE,
                         backbone_name=BACKBONE,
                         balanced_batches=BALANCED_BATCHES,
                         val_batch_size=VAL_BATCH_SIZE)
