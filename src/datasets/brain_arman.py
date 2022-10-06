from typing import Tuple

import numpy as np
from torch.utils.data import Dataset


class BrainArman(Dataset):
    def __init__(self,
                 base_path: str = '../../data/output_brain_arman/'):

        # Pre-load all the data to CPU. Saves time.
        with open('%s/output.npz' % base_path, 'rb') as f:
            npzfile = np.load(f)
            self.data_image = npzfile['data']
            self.data_label = npzfile['label'][:, :, :, 0]

        self.data_image = self.data_image / np.percentile(self.data_image, 99)
        self.data_image = np.where(self.data_image > 1., 1., self.data_image)
        self.data_image = np.where(self.data_image == 0, 1., self.data_image)
        self.data_image = (self.data_image * 2) - 1
        # channel last to channel first to comply with Torch.
        self.data_image = np.moveaxis(self.data_image, -1, 0)

        # Sanity check.
        assert self.data_image.shape[0] == self.data_label.shape[0], \
            'DiabeticMacularEdema Dataset have non-matching number of images (%s) and labels (%s)' \
            % (self.data_image.shape[0], self.data_label.shape[0])

    def __len__(self) -> int:
        return len(self.img_path)

    def __getitem__(self, idx) -> Tuple[np.array, np.array]:
        image = self.data_image[idx]
        label = self.data_label[idx]
        return image, label
