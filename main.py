import clip
import copy
import json
import clip.model
import kaolin as kal
import kaolin.ops.mesh
import numpy as np
import os
import random
import torch
import torch.nn as nn
import torchvision

from itertools import permutations, product
from Normalization import MeshNormalizer
from mesh import Mesh
from pointsCloud import PointsCloud
from pathlib import Path
from render import PointsCloudRenderer
from tqdm import tqdm
from torch.autograd import grad
from torchvision import transforms
from utils import device, color_mesh, color_points_cloud

class NeuralHighlighter(nn.Module):
    def __init__(self, depth, width, output_dim, input_dim=3):
        super(NeuralHighlighter, self).__init__()

        # Adapt input dim to the width of the model
        moduleList = nn.ModuleList([
            nn.Linear(input_dim, width),
            nn.ReLU(),
            nn.LayerNorm(width)
        ])

        # Append all the remaining layers
        for _ in range(depth):
            moduleList.extend([
                nn.Linear(width, width),
                nn.ReLU(),
                nn.LayerNorm(width)
            ])

        # Append last layer with softmax
        moduleList.extend([
            nn.Linear(width, output_dim),
            nn.Softmax(dim=1)
        ])

        self.mlp = moduleList
        print(self.mlp)
    
    def forward(self, x):
        for layer in self.mlp:
            x = layer(x)
        return x

def get_clip_model(clipmodel):
    return clip.load(clipmodel, device=device)
        

def clip_loss(clip_text: torch.Tensor, rendered_images: torch.Tensor, clip_model: clip.model.CLIP, n_augs: int, augment_transform: transforms.Compose) -> torch.Tensor:
    if n_augs == 0:
        clip_images: torch.Tensor = clip_model.encode_image(rendered_images)
        return -(torch.cosine_similarity(clip_text, torch.mean(clip_images)))
    
    loss = .0
    for _ in range(n_augs):
        augmented_images = augment_transform(rendered_images)
        clip_images: torch.Tensor = clip_model.encode_image(augmented_images)
        loss -= torch.cosine_similarity(clip_text, torch.mean(clip_images, dim=0, keepdim=True))

    return loss

    
def save_renders(dir, i, rendered_images, name=None):
    if name is not None:
        torchvision.utils.save_image(rendered_images, os.path.join(dir, name))
    else:
        torchvision.utils.save_image(rendered_images, os.path.join(dir, 'renders/iter_{}.jpg'.format(i)))


# Constrain most sources of randomness
# (some torch backwards functions within CLIP are non-determinstic)
torch.manual_seed(0)
torch.cuda.manual_seed(0)
torch.cuda.manual_seed_all(0)
random.seed(0)
np.random.seed(0)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True


render_res = 224
learning_rate = 0.0001
n_iter = 2500
res = 224
obj_path = 'data/horse.obj'
n_augs = 5
output_dir = './output/'
clip_model = 'ViT-L/14'
depth = 4
width = 256

Path(os.path.join(output_dir, 'renders')).mkdir(parents=True, exist_ok=True)

objbase, extension = os.path.splitext(os.path.basename(obj_path))

# Initialize variables
background = torch.tensor((1., 1., 1.)).to(device)

render = PointsCloudRenderer(background, dim=(render_res, render_res))
points_cloud = PointsCloud(obj_path, 10_000)

log_dir = output_dir


# MLP Settings
mlp = NeuralHighlighter(depth, width, 3).to(device)
optim = torch.optim.Adam(mlp.parameters(), learning_rate)

# list of possible colors
rgb_to_color = {(204/255, 1., 0.): "highlighter", (180/255, 180/255, 180/255): "gray"}
color_to_rgb = {"highlighter": [204/255, 1., 0.], "gray": [180/255, 180/255, 180/255]}
full_colors = [[204/255, 1., 0.], [180/255, 180/255, 180/255]]
colors = torch.tensor(full_colors).to(device)

# Transformations for images augmentations
augment_transform = transforms.Compose([
    transforms.RandomResizedCrop(res, scale=(1, 1)),
    transforms.RandomPerspective(fill=1, p=0.8, distortion_scale=0.5)
])


# --- Prompt ---
# encode prompt with CLIP
clip_model, _ = get_clip_model(clip_model)
obj = "horse"
region = "hat"
prompt = f"3D point cloud of a gray {obj} with bright green highlighted points on {region}."
with torch.no_grad():
    prompt_tokenize = clip.tokenize(prompt).to(device)
    clip_text = clip_model.encode_text(prompt_tokenize)


points = points_cloud.points_cloud.points_list()[0]

n_views = 5

losses = []

# Optimization loop
for i in tqdm(range(n_iter)):
    optim.zero_grad()

    # predict highlight probabilities
    pred_class = mlp(points)

    # color and render mesh
    sample_points_cloud = points_cloud
    sample_points_cloud.points_cloud = color_points_cloud(pred_class, points, colors)

    rendered_images = render.render_views(sample_points_cloud.points_cloud,
                                               num_views=n_views,
                                               std=1)

    # Calculate CLIP Loss
    loss = clip_loss(clip_text, rendered_images, clip_model, n_augs, augment_transform)
    loss.backward(retain_graph=True)

    optim.step()

    # update variables + record loss
    with torch.no_grad():
        losses.append(loss.item())

    # report results
    if i % 100 == 0:
        print("Last 100 CLIP score: {}".format(np.mean(losses[-100:])))
        save_renders(log_dir, i, rendered_images)
        with open(os.path.join(log_dir, "training_info.txt"), "a") as f:
            f.write(f"For iteration {i}... Prompt: {prompt}, Last 100 avg CLIP score: {np.mean(losses[-100:])}, CLIP score {losses[-1]}\n")


# save results
sample_points_cloud.save(f"{output_dir}{prompt.replace(' ', '_')}.ply")

# Save prompts
# with open(os.path.join(dir(), prompt), "w") as f:
#     f.write('')