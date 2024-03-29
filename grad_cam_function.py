import argparse
import cv2
import numpy as np
import torch
from torch.autograd import Function
from torchvision import models
from PIL import Image, ImageOps
from model.model_Grad_CAM import CovidNet_Grad_CAM
from torchvision import transforms
import os

class FeatureExtractor():
    """ Class for extracting activations and 
    registering gradients from targetted intermediate layers """

    def __init__(self, model, target_layers):
        self.model = model
        self.target_layers = target_layers
        self.gradients = []

    def save_gradient(self, grad):
        self.gradients.append(grad)

    def __call__(self, x):
        outputs = []
        self.gradients = []
        
        #x = RecorrerCOVIDNet(self.model,x)
        x = self.model(x)
        x.register_hook(self.save_gradient)
        outputs += [x]
        
        '''
        for name, module in self.model._modules.items():
            print(name)
            try:
                x = module(x)
                #print('Modulos validados')
                #print(name)
                if name in self.target_layers:
                    x.register_hook(self.save_gradient)
                    outputs += [x]
            except:
                print('Sub-modulo secuencial no ejecutable sobre la imagen de entrada')
                #print(name)
        '''
        return outputs, x


class ModelOutputs():
    """ Class for making a forward pass, and getting:
    1. The network output.
    2. Activations from intermeddiate targetted layers.
    3. Gradients from intermeddiate targetted layers. """

    def __init__(self, model, target_layers):
        self.model = model
        self.feature_extractor = FeatureExtractor(self.model.features, target_layers)

    def get_gradients(self):
        return self.feature_extractor.gradients

    def __call__(self, x):
        target_activations, output = self.feature_extractor(x)
        #output = output.view(output.size(0), -1)
        #print(target_activations[0].shape)
        #print(output.shape)
        #output = torch.flatten(output)
        output = self.model.classifier(output)
        return target_activations, output

#def RecorrerCOVIDNet(model,x):

def preprocess_image(img):
    means = [0.485, 0.456, 0.406]
    stds = [0.229, 0.224, 0.225]

    preprocessed_img = img.copy()[:, :, ::-1]
    for i in range(3):
        preprocessed_img[:, :, i] = preprocessed_img[:, :, i] - means[i]
        preprocessed_img[:, :, i] = preprocessed_img[:, :, i] / stds[i]
    preprocessed_img = \
        np.ascontiguousarray(np.transpose(preprocessed_img, (2, 0, 1)))
    preprocessed_img = torch.from_numpy(preprocessed_img)
    preprocessed_img.unsqueeze_(0)
    input = preprocessed_img.requires_grad_(True)
    return input


def show_cam_on_image(img, mask, file_name):
    heatmap = cv2.applyColorMap(np.uint8(255 * mask), cv2.COLORMAP_JET)
    heatmap = np.float32(heatmap) / 255
    cam = heatmap + np.float32(img)
    cam = cam / np.max(cam)
    #cv2.imwrite("cam.jpg", np.uint8(255 * cam))
    cv2.imwrite(file_name, np.uint8(255 * cam))


class GradCam:
    def __init__(self, model, target_layer_names, use_cuda):
        self.model = model
        self.model.eval()
        self.cuda = use_cuda
        if self.cuda:
            self.model = model.cuda()

        self.extractor = ModelOutputs(self.model, target_layer_names)

    def forward(self, input):
        return self.model(input)

    def __call__(self, input, index=None):
        if self.cuda:
            features, output = self.extractor(input.cuda())
        else:
            features, output = self.extractor(input)

        if index == None:
            index = np.argmax(output.cpu().data.numpy())

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0][index] = 1
        one_hot = torch.from_numpy(one_hot).requires_grad_(True)
        if self.cuda:
            one_hot = torch.sum(one_hot.cuda() * output)
        else:
            one_hot = torch.sum(one_hot * output)

        self.model.features.zero_grad()
        self.model.classifier.zero_grad()
        one_hot.backward(retain_graph=True)

        grads_val = self.extractor.get_gradients()[-1].cpu().data.numpy()

        target = features[-1]
        target = target.cpu().data.numpy()[0, :]

        weights = np.mean(grads_val, axis=(2, 3))[0, :]
        cam = np.zeros(target.shape[1:], dtype=np.float32)

        for i, w in enumerate(weights):
            cam += w * target[i, :, :]

        cam = np.maximum(cam, 0)
        cam = cv2.resize(cam, (224, 224))
        cam = cam - np.min(cam)
        cam = cam / np.max(cam)
        return cam


class GuidedBackpropReLU(Function):

    @staticmethod
    def forward(self, input):
        positive_mask = (input > 0).type_as(input)
        output = torch.addcmul(torch.zeros(input.size()).type_as(input), input, positive_mask)
        self.save_for_backward(input, output)
        return output

    @staticmethod
    def backward(self, grad_output):
        input, output = self.saved_tensors
        grad_input = None

        positive_mask_1 = (input > 0).type_as(grad_output)
        positive_mask_2 = (grad_output > 0).type_as(grad_output)
        grad_input = torch.addcmul(torch.zeros(input.size()).type_as(input),
                                   torch.addcmul(torch.zeros(input.size()).type_as(input), grad_output,
                                                 positive_mask_1), positive_mask_2)

        return grad_input


class GuidedBackpropReLUModel:
    def __init__(self, model, use_cuda):
        self.model = model
        self.model.eval()
        self.cuda = use_cuda
        if self.cuda:
            self.model = model.cuda()

        # replace ReLU with GuidedBackpropReLU
        for idx, module in self.model.features._modules.items():
            if module.__class__.__name__ == 'ReLU':
                self.model.features._modules[idx] = GuidedBackpropReLU.apply

    def forward(self, input):
        return self.model(input)

    def __call__(self, input, index=None):
        if self.cuda:
            output = self.forward(input.cuda())
        else:
            output = self.forward(input)

        if index == None:
            index = np.argmax(output.cpu().data.numpy())

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0][index] = 1
        one_hot = torch.from_numpy(one_hot).requires_grad_(True)
        if self.cuda:
            one_hot = torch.sum(one_hot.cuda() * output)
        else:
            one_hot = torch.sum(one_hot * output)

        # self.model.features.zero_grad()
        # self.model.classifier.zero_grad()
        one_hot.backward(retain_graph=True)

        output = input.grad.cpu().data.numpy()
        output = output[0, :, :, :]

        return output


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--use-cuda', action='store_true', default=False,
                        help='Use NVIDIA GPU acceleration')
    parser.add_argument('--image-path', type=str, default='./examples/both.png',
                        help='Input image path')
    parser.add_argument('--saved_model', type=str, default='/home/julian/Documents/PythonExperiments/COVIDNet/ModelSavedCoviNet/COVIDNet20200406_0412/COVIDNet_best_checkpoint.pth.tar',
                        help='path to save_model ')
    parser.add_argument('--path_save', type=str, default='/home/julian/Documents/PythonExperiments/COVIDNet/Grad_CAM_Images/',
                        help='path to images generated by Grad_CAM ')
    args = parser.parse_args()
    args.use_cuda = args.use_cuda and torch.cuda.is_available()
    if args.use_cuda:
        print("Using GPU for acceleration")
    else:
        print("Using CPU for computation")

    return args

def deprocess_image(img):
    """ see https://github.com/jacobgil/keras-grad-cam/blob/master/grad-cam.py#L65 """
    img = img - np.mean(img)
    img = img / (np.std(img) + 1e-5)
    img = img * 0.1
    img = img + 0.5
    img = np.clip(img, 0, 1)
    return np.uint8(img*255)

def load_model(path_model,n_clases=3):
    checkpoint = torch.load(path_model)
    model = CovidNet_Grad_CAM(n_clases)
    model.load_state_dict(checkpoint['state_dict'])
    return model

def load_image(img_path):
    if not os.path.exists(img_path):
        print("IMAGE DOES NOT EXIST {}".format(img_path))
    image = cv2.imread(img_path)
    image2 = np.copy(image)
    image2[image2>0]=255
    image2 = image2[:,:,0]
    mask = Image.fromarray(image2.astype('uint8'))
  
    img_adapteq1 = Image.fromarray(image.astype('uint8'), 'RGB')
    img_adapteq = ImageOps.equalize(img_adapteq1,mask=mask)
    #-------------------------------------------------
    image2 = img_adapteq1.resize((224, 224))
    image2 = np.array(image2)
    image2 = image2.astype('float32') / np.max(image2)
    #--------------------------------------------------
    preprocess = transforms.Compose([
        transforms.Resize(224),
        transforms.CenterCrop(224),
        transforms.ToTensor(),#normaliza a [0,1]
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])
    image_tensor = preprocess(img_adapteq)
    image_tensor = image_tensor.unsqueeze(0)

    return image_tensor.requires_grad_(True),image2


def main(args):
    """ python grad_cam.py <path_to_image>
    1. Loads an image with opencv.
    2. Preprocesses it for VGG19 and converts to a pytorch variable.
    3. Makes a forward pass to find the category index with the highest score,
    and computes intermediate activations.
    Makes the visualization. """

    #args = get_args()

    # Can work with any model, but it assumes that the model has a
    # feature method, and a classifier method,
    # as in the VGG models in torchvision.
    grad_cam = GradCam(model=load_model(args.saved_model), \
                       target_layer_names=["Output"], use_cuda=args.use_cuda)

    #img = cv2.imread(args.image_path, 1)
    #img = np.float32(cv2.resize(img, (224, 224))) / 255
    #input = preprocess_image(img)
    #print(input.shape)
    input, img = load_image(args.image_path)


     # If None, returns the map for the highest scoring category.
    # Otherwise, targets the requested index.
    target_index = None
    mask = grad_cam(input, target_index)
    #-------------------------------------------------------------
    image_name = args.image_path.split('/')
    #image_name = image_name[-1].split('.')
    file_name1 = args.path_save + 'cam_' + image_name[-1] #+ '.jpg'
    #-------------------------------------------------------------
    show_cam_on_image(img, mask, file_name1)

    gb_model = GuidedBackpropReLUModel(model=load_model(args.saved_model), use_cuda=args.use_cuda)
    gb = gb_model(input, index=target_index)
    gb = gb.transpose((1, 2, 0))
    cam_mask = cv2.merge([mask, mask, mask])
    cam_gb = deprocess_image(cam_mask*gb)
    gb = deprocess_image(gb)

    

    file_name1 = args.path_save + 'gb_' + image_name[-1] #+ '.jpg'
    file_name2 = args.path_save + 'cam_gb_' + image_name[-1] #+ '.jpg'
    cv2.imwrite(file_name1, gb)
    cv2.imwrite(file_name2, cam_gb)
    #cv2.imwrite('Grad_CAM_Images/gb.jpg', gb)
    #cv2.imwrite('Grad_CAM_Images/cam_gb.jpg', cam_gb)