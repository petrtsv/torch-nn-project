import random

import torchvision
from torchvision.datasets import ImageFolder
from torchvision.transforms import transforms

import data

resize = transforms.Compose(
    [
        transforms.Resize(224),
        transforms.CenterCrop(224),
    ]
)

augment = transforms.Compose(
    [
        transforms.RandomRotation(degrees=15),
        transforms.RandomHorizontalFlip(p=0.5),
        transforms.ColorJitter(),
        transforms.RandomPerspective(p=0.2, distortion_scale=0.25),
    ]
)

normalize = transforms.Compose(
    [
        transforms.ToTensor(),
        transforms.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225))
    ]
)


class ImageItem(object):
    def __init__(self, source: ImageFolder, index: int):
        self.source = source
        self.index = index

    def load(self):
        return self.source[self.index][0]


class MiniImageNetDataset(data.LabeledDataset):
    CLASSES = 100

    def __init__(self, root="C:\\datasets\\mini-imagenet\\train", augment_prob=0.0, reduce=0.0,
                 random_seed=42):
        self.reduce = reduce
        random.seed(random_seed)

        self.test_transform = transforms.Compose(
            [
                resize,
                normalize
            ]
        )
        self.train_transform = transforms.Compose(
            [
                resize,
                transforms.RandomApply([augment], p=augment_prob),
                normalize
            ]
        )

        self.source_dataset_train = torchvision.datasets.ImageFolder(root=root)

        self.dataset_train_size = len(self.source_dataset_train)
        items = []
        labels = []
        for i in range(self.dataset_train_size):
            items.append(ImageItem(self.source_dataset_train, i))
            labels.append(self.source_dataset_train[i][1])
        is_test = [0] * self.dataset_train_size

        super(MiniImageNetDataset, self).__init__(items, labels, is_test)

        self.train_subdataset, self.test_subdataset = self.subdataset.train_test_split()

        if reduce < 1:
            self.train_subdataset = self.train_subdataset.downscale(1 - reduce)
        else:
            self.train_subdataset = self.train_subdataset.balance(reduce)

    def __getitem__(self, item):
        image, label, is_test = super(MiniImageNetDataset, self).__getitem__(item)
        if is_test:
            image = self.test_transform(image)
        else:
            image = self.train_transform(image)

        return image, label, is_test

    def label_stat(self):
        pass

    def train(self):
        return self.train_subdataset

    def test(self):
        return self.test_subdataset