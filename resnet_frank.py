import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl
from torchmetrics import Accuracy, MeanAbsoluteError
from torchvision.models.video import r3d_18, R3D_18_Weights
class FrankWolfeOptimizer:
    def __init__(self, num_tasks=2, max_iter=5):
        self.num_tasks = num_tasks
        self.max_iter = max_iter
        self.weights = None
        self.device = None
        
    def to(self, device):
        """Move weights to specified device"""
        self.device = device
        if self.weights is None:
            self.weights = (torch.ones(self.num_tasks) / self.num_tasks).to(device)
        else:
            self.weights = self.weights.to(device)
        return self
        
    def compute_gradient(self, losses):
        """Compute gradient for each loss"""
        return torch.stack([loss.detach() for loss in losses])
    
    def solve_linear_problem(self, gradients):
        """Solve linear optimization problem"""
        min_idx = torch.argmin(gradients)
        s = torch.zeros_like(self.weights, device=self.device)
        s[min_idx] = 1.0
        return s
    
    def update_weights(self, losses):
        """Update weights using Frank-Wolfe algorithm"""
        if self.weights is None:
            self.device = losses[0].device
            self.weights = (torch.ones(self.num_tasks) / self.num_tasks).to(self.device)
            
        gradients = self.compute_gradient(losses)
        s = self.solve_linear_problem(gradients)
        
        gamma = 2.0 / (self.max_iter + 2)
        self.weights = (1 - gamma) * self.weights + gamma * s
        
        self.weights = F.softmax(self.weights, dim=0)
        
        return self.weights

class AttentionGatingModule(nn.Module):
    def __init__(self, feature_dim):
        super(AttentionGatingModule, self).__init__()
        self.attention = nn.Sequential(
            nn.Linear(feature_dim, feature_dim // 2),
            nn.ReLU(),
            nn.Linear(feature_dim // 2, feature_dim),
            nn.Sigmoid()
        )

    def forward(self, shared_features, task_specific_features):
        if shared_features.size(1) != task_specific_features.size(1):
            task_specific_features = F.linear(task_specific_features, 
                                           torch.eye(shared_features.size(1)).to(task_specific_features.device))
        
        attention_weights = self.attention(shared_features)
        gated_features = shared_features * attention_weights + \
                        task_specific_features * (1 - attention_weights)
        return gated_features

class MultiTaskAlzheimerModel(pl.LightningModule):
    def __init__(self, 
                 num_classes=2, 
                 input_shape=(1, 64, 64, 64),
                 metadata_dim=3,
                 pretrained=True):
        super(MultiTaskAlzheimerModel, self).__init__()
        
        if pretrained:
            print('dfdfdfdfdff===================================')
            self.backbone = r3d_18(weights=R3D_18_Weights.DEFAULT)
        else:
            self.backbone = r3d_18(weights=None)
        self.backbone.stem[0] = nn.Conv3d(
            input_shape[0], 64, 
            kernel_size=(3, 7, 7), 
            stride=(1, 2, 2), 
            padding=(1, 3, 3), 
            bias=False
        )
        
        # Remove the final classification layer
        self.backbone = nn.Sequential(*list(self.backbone.children())[:-1])
        
        # Metadata processing
        self.metadata_embedding = nn.Sequential(
            nn.Linear(metadata_dim, 128),
            nn.LayerNorm(128),
            nn.ReLU(inplace=True)
        )
        
        # Cross attention for feature fusion
        self.cross_attention = nn.Sequential(
            nn.Linear(512 + 128, 512),  # ResNet18 has 512 features
            nn.LayerNorm(512),
            nn.ReLU(inplace=True)
        )
        
        # Shared representation
        self.shared_representation = nn.Sequential(
            nn.Linear(512, 1024),
            nn.LayerNorm(1024),
            nn.ReLU(inplace=True),
            nn.Dropout(0.5)
        )
        
        # Task-specific branches
        self.classification_gate = AttentionGatingModule(1024)
        self.classification_branch = nn.Sequential(
            nn.Linear(1024, 512),
            nn.ReLU(inplace=True),
            nn.Dropout(0.5),
            nn.Linear(512, num_classes)
        )
        
        self.regression_gate = AttentionGatingModule(1024)
        self.regression_branch = nn.Sequential(
            nn.Linear(1024, 512),
            nn.ReLU(inplace=True),
            nn.Dropout(0.5),
            nn.Linear(512, 1)
        )
        
        # Metrics
        self.train_classification_accuracy = Accuracy(task='multiclass', num_classes=num_classes)
        self.val_classification_accuracy = Accuracy(task='multiclass', num_classes=num_classes)
        self.test_classification_accuracy = Accuracy(task='multiclass', num_classes=num_classes)
        self.mmse_mae = MeanAbsoluteError()
        
        # Losses
        self.classification_loss = nn.CrossEntropyLoss(label_smoothing=0.1)
        self.regression_loss = nn.HuberLoss()
        self.multi_task_optimizer = FrankWolfeOptimizer(num_tasks=2)
        
        # Loss history
        self.loss_history = {
            'classification': [],
            'regression': [],
            'weights': []
        }

    def on_fit_start(self):
        """Called when training begins"""
        self.multi_task_optimizer.to(self.device)

    def forward(self, image, metadata):
        # Extract features using ResNet3D
        x = self.backbone(image)
        image_features = x.squeeze(-1).squeeze(-1).squeeze(-1)  # Remove spatial dimensions
        
        # Process metadata
        metadata_features = self.metadata_embedding(metadata)
        
        # Feature fusion
        fused_features = self.cross_attention(torch.cat([image_features, metadata_features], dim=1))
        
        # Shared representation
        shared_features = self.shared_representation(fused_features)
        
        # Task-specific outputs
        classification_features = self.classification_gate(shared_features, shared_features)
        classification_output = self.classification_branch(classification_features)
        
        regression_features = self.regression_gate(shared_features, shared_features)
        regression_output = self.regression_branch(regression_features)
        
        return classification_output, regression_output

    def training_step(self, batch, batch_idx):
        image, label, mmse, age, gender = batch['image'], batch['label'], batch['mmse'], batch['age'], batch['gender']
        metadata = torch.stack([mmse, age, gender], dim=1).float()
        
        classification_output, regression_output = self(image, metadata)
        
        classification_loss = self.classification_loss(classification_output, label)
        regression_loss = self.regression_loss(regression_output.squeeze(), mmse)
        
        losses = [classification_loss.to(self.device), regression_loss.to(self.device)]
        weights = self.multi_task_optimizer.update_weights(losses)
        
        total_loss = torch.sum(torch.stack(losses) * weights)
        
        preds = torch.argmax(classification_output, dim=1)
        acc = (preds == label).float().mean()
        
        # Logging
        self.log('train_loss', total_loss, on_step=True, on_epoch=True, prog_bar=True)
        self.log('train_classification_loss', classification_loss, on_step=True, on_epoch=True)
        self.log('train_regression_loss', regression_loss, on_step=True, on_epoch=True)
        self.log('train_classification_acc', acc, on_step=True, on_epoch=True, prog_bar=True)
        self.log('train_classification_weight', weights[0], on_step=True, on_epoch=True)
        self.log('train_regression_weight', weights[1], on_step=True, on_epoch=True)
        
        # Save history
        self.loss_history['classification'].append(classification_loss.item())
        self.loss_history['regression'].append(regression_loss.item())
        self.loss_history['weights'].append(weights.tolist())
        
        return total_loss

    def validation_step(self, batch, batch_idx):
        image, label, mmse, age, gender = batch['image'], batch['label'], batch['mmse'], batch['age'], batch['gender']
        metadata = torch.stack([mmse, age, gender], dim=1).float()
        
        classification_output, regression_output = self(image, metadata)
        
        classification_loss = self.classification_loss(classification_output, label)
        regression_loss = self.regression_loss(regression_output.squeeze(), mmse)
        
        losses = [classification_loss.to(self.device), regression_loss.to(self.device)]
        weights = self.multi_task_optimizer.weights
        
        total_loss = torch.sum(torch.stack(losses) * weights)
        
        preds = torch.argmax(classification_output, dim=1)
        acc = (preds == label).float().mean()
        
        # Logging
        self.log('val_loss', total_loss, on_epoch=True, prog_bar=True)
        self.log('val_classification_loss', classification_loss, on_epoch=True)
        self.log('val_regression_loss', regression_loss, on_epoch=True)
        self.log('val_classification_acc', acc, on_epoch=True, prog_bar=True)
        self.log('val_classification_weight', weights[0], on_epoch=True)
        self.log('val_regression_weight', weights[1], on_epoch=True)
        
        return total_loss

    def test_step(self, batch, batch_idx):
        image, label, mmse, age, gender = batch['image'], batch['label'], batch['mmse'], batch['age'], batch['gender']
        metadata = torch.stack([mmse, age, gender], dim=1).float()
        
        classification_output, regression_output = self(image, metadata)
        
        classification_loss = self.classification_loss(classification_output, label).to(self.device)
        regression_loss = self.regression_loss(regression_output.squeeze(), mmse).to(self.device)
        
        weights = self.multi_task_optimizer.weights
        total_loss = torch.sum(torch.stack([classification_loss, regression_loss]) * weights)
        
        preds = torch.argmax(classification_output, dim=1)
        acc = (preds == label).float().mean()
        mae = F.l1_loss(regression_output.squeeze(), mmse)
        
        # Detailed metrics logging
        self.log('test_total_loss', total_loss, on_epoch=True)
        self.log('test_classification_loss', classification_loss, on_epoch=True)
        self.log('test_regression_loss', regression_loss, on_epoch=True)
        self.log('test_classification_acc', acc, on_epoch=True, prog_bar=True)
        self.log('test_regression_mae', mae, on_epoch=True, prog_bar=True)
        self.log('final_classification_weight', weights[0], on_epoch=True)
        self.log('final_regression_weight', weights[1], on_epoch=True)
        
        return {
            'preds': preds,
            'true_labels': label,
            'predicted_mmse': regression_output.squeeze(),
            'true_mmse': mmse,
            'final_weights': weights.cpu()
        }

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, 
            mode='min', 
            factor=0.1, 
            patience=5
        )
        return {
            'optimizer': optimizer,
            'lr_scheduler': {
                'scheduler': scheduler,
                'monitor': 'val_loss'
            }
        }