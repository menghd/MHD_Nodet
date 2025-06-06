"""
MHD_Nodet Project - Training Module
===================================
This module implements the training pipeline for the MHD_Nodet project, integrating network, dataset, and evaluation components.
- Supports custom data loading from separate train and val directories, and batch-consistent augmentations.
- Includes learning rate scheduling (warmup + cosine annealing) and early stopping for robust training.

项目：MHD_Nodet - 训练模块
本模块实现 MHD_Nodet 项目的训练流水线，集成网络、数据集和评估组件。
- 支持从单独的 train 和 val 目录加载自定义数据，以及批次一致的数据增强。
- 包含学习率调度（预热 + 余弦退火）和早停机制以确保稳健训练。

Author: Souray Meng (孟号丁)
Email: souray@qq.com
Institution: Tsinghua University (清华大学)
"""

import os
import torch
import torch.optim as optim
from torch.utils.data import DataLoader
import numpy as np
import json
import logging
import sys
sys.path.append(r"C:\Users\souray\Desktop\Codes")
from node_toolkit.new_node_net import MHDNet, HDNet
from node_toolkit.node_dataset import NodeDataset, MinMaxNormalize, ZScoreNormalize, RandomRotate, RandomFlip, RandomShift, RandomZoom, OneHot, OrderedSampler, worker_init_fn
from node_toolkit.node_utils import train, validate, WarmupCosineAnnealingLR
from node_toolkit.node_results import (
    node_lp_loss, node_focal_loss, node_dice_loss, node_iou_loss,
    node_recall_metric, node_precision_metric, node_f1_metric, node_dice_metric, node_iou_metric, node_mse_metric
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    """
    Main function to run the training pipeline.
    运行训练流水线的主函数。
    """
    seed = 4
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)

    # Data and save paths
    base_data_dir = r"C:\Users\souray\Desktop\new_Tr"
    train_data_dir = os.path.join(base_data_dir, "train")
    val_data_dir = os.path.join(base_data_dir, "val")
    save_dir = r"C:\Users\souray\Desktop\MHDNet0422"
    os.makedirs(save_dir, exist_ok=True)

    # Hyperparameters
    batch_size = 4
    num_dimensions = 3
    num_epochs = 200
    learning_rate = 1e-3
    validation_interval = 1
    patience = 200
    warmup_epochs = 10
    num_workers = 0

    # Subnetwork 12 (Segmentation task: Plaque, binary segmentation)
    node_configs_segmentation = {
        0: (1, 64, 64, 64), 1: (1, 64, 64, 64), 2: (1, 64, 64, 64), 3: (1, 64, 64, 64), 4: (2, 64, 64, 64),
        5: (64, 64, 64, 64), 6: (128, 32, 32, 32), 7: (64, 64, 64, 64), 8: (2, 64, 64, 64), 9: (1, 64, 64, 64)
    }
    hyperedge_configs_segmentation = {
        "e1": {"src_nodes": [0, 1, 2, 3, 4], "dst_nodes": [5], "params": {
            "convs": [torch.Size([64, 6, 3, 3, 3]), torch.Size([64, 64, 3, 3, 3])],
            "reqs": [True, True],
            "norms": ["batch", "batch"],
            "acts": ["leakyrelu", "leakyrelu"],
            "feature_size": (64, 64, 64)}},
        "e2": {"src_nodes": [5], "dst_nodes": [6], "params": {
            "convs": [torch.Size([128, 64, 3, 3, 3]), torch.Size([128, 128, 3, 3, 3])],
            "reqs": [True, True],
            "norms": ["batch", "batch"],
            "acts": ["leakyrelu", "leakyrelu"],
            "feature_size": (32, 32, 32),
            "intp": 1}},
        "e3": {"src_nodes": [5, 6], "dst_nodes": [7], "params": {
            "convs": [torch.Size([64, 192, 3, 3, 3]), torch.Size([64, 64, 3, 3, 3])],
            "reqs": [True, True],
            "norms": ["batch", "batch"],
            "acts": ["leakyrelu", "leakyrelu"],
            "feature_size": (64, 64, 64),
            "intp": 1}},
        "e4": {"src_nodes": [7], "dst_nodes": [8], "params": {
            "convs": [torch.Size([2, 64, 3, 3, 3])],
            "reqs": [True],
            "norms": ["batch"],
            "acts": ["relu"],
            "feature_size": (64, 64, 64)}},
        "e5": {"src_nodes": [4], "dst_nodes": [8], "params": {
            "convs": [torch.eye(2).reshape(2, 2, 1, 1, 1)],
            "reqs": [False],
            "norms": [None],
            "acts": [None],
            "feature_size": (64, 64, 64)}},
        "e6": {"src_nodes": [0], "dst_nodes": [9], "params": {
            "convs": [torch.eye(1).reshape(1, 1, 1, 1, 1)],
            "reqs": [False],
            "norms": [None],
            "acts": [None],
            "feature_size": (64, 64, 64)}},
    }
    in_nodes_segmentation = [0, 1, 2, 3, 4]
    out_nodes_segmentation = [0, 1, 2, 3, 4, 8, 9]

    # Subnetwork 13 (Target node for reshaped features)
    node_configs_target = {
        0: (2, 64, 64, 64)
    }
    hyperedge_configs_target = {}
    in_nodes_target = [0]
    out_nodes_target = [0]

    # Global node mapping
    node_mapping = [
        (100, "segmentation", 0), (101, "segmentation", 1),
        (102, "segmentation", 2), (103, "segmentation", 3), (104, "segmentation", 4),
        (508, "segmentation", 8), (509, "segmentation", 9),
        (600, "target", 0)
    ]

    # Instantiate subnetworks
    sub_networks_configs = {
        "segmentation": (node_configs_segmentation, hyperedge_configs_segmentation, in_nodes_segmentation, out_nodes_segmentation),
        "target": (node_configs_target, hyperedge_configs_target, in_nodes_target, out_nodes_target),
    }
    sub_networks = {
        name: HDNet(node_configs, hyperedge_configs, in_nodes, out_nodes, num_dimensions)
        for name, (node_configs, hyperedge_configs, in_nodes, out_nodes) in sub_networks_configs.items()
    }

    # Global input and output nodes
    in_nodes = [100, 101, 102, 103, 104, 600]
    out_nodes = [100, 101, 102, 103, 104, 508, 509, 600]

    # Node suffix mapping
    node_suffix = [
        (100, "0000"), (101, "0001"), (102, "0002"), (103, "0003"), (104, "0004"),
        (600, "0004")
    ]

    # Instantiate transformations
    random_rotate1 = RandomRotate(max_angle=5)
    random_rotate2 = RandomRotate(max_angle=5)
    random_flip = RandomFlip()
    random_shift = RandomShift(max_shift=5)
    random_zoom1 = RandomZoom(zoom_range=(0.9, 1.1))
    random_zoom2 = RandomZoom(zoom_range=(0.9, 1.1))
    random_zoom3 = RandomZoom(zoom_range=(0.9, 1.1))
    min_max_normalize = MinMaxNormalize()
    z_score_normalize = ZScoreNormalize()
    one_hot = OneHot(num_classes=2)

    # Node transformation configuration for train and validate
    node_transforms = {
        "train": {
            100: [random_rotate1, random_flip, random_shift, random_zoom1, min_max_normalize, z_score_normalize],
            101: [random_rotate1, random_flip, random_shift, random_zoom2, min_max_normalize, z_score_normalize],
            102: [random_rotate1, random_flip, random_shift, random_zoom3, min_max_normalize, z_score_normalize],
            103: [random_rotate1, random_flip, random_shift, random_zoom1, min_max_normalize, z_score_normalize],
            104: [random_rotate2, random_flip, random_shift, random_zoom2, one_hot],
            600: [random_rotate2, random_flip, random_shift, random_zoom2, one_hot],
            601: [], 602: [], 603: [], 604: [], 605: [], 606: [], 607: [], 608: [], 609: [],
        },
        "validate": {
            100: [min_max_normalize, z_score_normalize],
            101: [min_max_normalize, z_score_normalize],
            102: [min_max_normalize, z_score_normalize],
            103: [min_max_normalize, z_score_normalize],
            104: [one_hot],
            600: [one_hot],
            601: [], 602: [], 603: [], 604: [], 605: [], 606: [], 607: [], 608: [], 609: [],
        }
    }

    # Task configuration
    task_configs = {
        "segmentation_plaque": {
            "loss": [
                {"fn": node_dice_loss, "src_node": 508, "target_node": 600, "weight": 1.0, "params": {}},
                {"fn": node_dice_loss, "src_node": 104, "target_node": 600, "weight": 1.0, "params": {}},
                {"fn": node_iou_loss, "src_node": 508, "target_node": 600, "weight": 0.5, "params": {}},
                {"fn": node_iou_loss, "src_node": 104, "target_node": 600, "weight": 0.5, "params": {}},
                {"fn": node_lp_loss, "src_node": 104, "target_node": 600, "weight": 0.5, "params": {}},
                {"fn": node_lp_loss, "src_node": 103, "target_node": 100, "weight": 0.5, "params": {}},
                {"fn": node_lp_loss, "src_node": 103, "target_node": 101, "weight": 0.5, "params": {}},
                {"fn": node_lp_loss, "src_node": 103, "target_node": 102, "weight": 0.5, "params": {}},
                {"fn": node_lp_loss, "src_node": 508, "target_node": 600, "weight": 0.5, "params": {}},
                {"fn": node_lp_loss, "src_node": 100, "target_node": 509, "weight": 0.5, "params": {}},
            ],
            "metric": [
                {"fn": node_dice_metric, "src_node": 508, "target_node": 600, "params": {}},
                {"fn": node_iou_metric, "src_node": 508, "target_node": 600, "params": {}},
                {"fn": node_recall_metric, "src_node": 508, "target_node": 600, "params": {}},
                {"fn": node_precision_metric, "src_node": 508, "target_node": 600, "params": {}},
                {"fn": node_f1_metric, "src_node": 508, "target_node": 600, "params": {}},
            ],
        },
    }

    # Collect case IDs for train and val
    def get_case_ids(data_dir, suffix, file_ext):
        all_files = sorted(os.listdir(data_dir))
        case_ids = set()
        for file in all_files:
            if file.startswith('case_') and file.endswith(f'_{suffix}{file_ext}'):
                case_id = file.split('_')[1]
                case_ids.add(case_id)
        return sorted(list(case_ids))

    # Initialize suffix to nodes mapping
    suffix_to_nodes = {}
    for node, suffix in node_suffix:
        if suffix not in suffix_to_nodes:
            suffix_to_nodes[suffix] = []
        suffix_to_nodes[suffix].append(node)

    # Get case IDs for train and val directories
    train_suffix_case_ids = {}
    val_suffix_case_ids = {}
    for suffix in suffix_to_nodes:
        train_suffix_case_ids[suffix] = get_case_ids(train_data_dir, suffix, '.nii.gz') or get_case_ids(train_data_dir, suffix, '.csv')
        val_suffix_case_ids[suffix] = get_case_ids(val_data_dir, suffix, '.nii.gz') or get_case_ids(val_data_dir, suffix, '.csv')

    # Find common case IDs
    train_common_case_ids = set.intersection(*(set(case_ids) for case_ids in train_suffix_case_ids.values()))
    val_common_case_ids = set.intersection(*(set(case_ids) for case_ids in val_suffix_case_ids.values()))
    if not train_common_case_ids:
        raise ValueError("No common case_ids found in train directory!")
    if not val_common_case_ids:
        raise ValueError("No common case_ids found in val directory!")
    train_case_ids = sorted(list(train_common_case_ids))
    val_case_ids = sorted(list(val_common_case_ids))

    # Log incomplete cases
    for suffix, case_ids in train_suffix_case_ids.items():
        missing = set(case_ids) - train_common_case_ids
        if missing:
            logger.warning(f"Incomplete train cases for suffix {suffix}: {sorted(list(missing))}")
    for suffix, case_ids in val_suffix_case_ids.items():
        missing = set(case_ids) - val_common_case_ids
        if missing:
            logger.warning(f"Incomplete val cases for suffix {suffix}: {sorted(list(missing))}")

    # Generate global random order for training
    train_case_id_order = np.random.permutation(train_case_ids).tolist()
    val_case_id_order = val_case_ids

    # Save data split information
    split_info = {
        "train_case_ids": train_case_ids,
        "val_case_ids": val_case_ids,
        "train_case_id_order": train_case_id_order,
        "val_case_id_order": val_case_id_order,
        "train_count": len(train_case_ids),
        "val_count": len(val_case_ids),
    }
    split_save_path = os.path.join(save_dir, "data_split.json")
    with open(split_save_path, "w") as f:
        json.dump(split_info, f, indent=4)
    logger.info(f"Data split saved to {split_save_path}")

    # Create datasets
    datasets_train = {}
    datasets_val = {}
    for node, suffix in node_suffix:
        target_shape = None
        for global_node, sub_net_name, sub_node_id in node_mapping:
            if global_node == node:
                target_shape = sub_networks[sub_net_name].node_configs[sub_node_id]
                break
        if target_shape is None:
            raise ValueError(f"Node {node} not found in node_mapping")
        datasets_train[node] = NodeDataset(
            train_data_dir, node, suffix, target_shape, node_transforms["train"].get(node, []),
            node_mapping=node_mapping, sub_networks=sub_networks,
            case_ids=train_case_ids, case_id_order=train_case_id_order,
            num_dimensions=num_dimensions
        )
        datasets_val[node] = NodeDataset(
            val_data_dir, node, suffix, target_shape, node_transforms["validate"].get(node, []),
            node_mapping=node_mapping, sub_networks=sub_networks,
            case_ids=val_case_ids, case_id_order=val_case_id_order,
            num_dimensions=num_dimensions
        )

    # Validate case_id_order consistency across nodes
    for node in datasets_train:
        if datasets_train[node].case_ids != datasets_train[list(datasets_train.keys())[0]].case_ids:
            raise ValueError(f"Case ID order inconsistent for node {node}")
        if datasets_val[node].case_ids != datasets_val[list(datasets_val.keys())[0]].case_ids:
            raise ValueError(f"Case ID order inconsistent for node {node} in validation")

    # Create DataLoaders with custom sampler and worker initialization
    dataloaders_train = {}
    dataloaders_val = {}
    for node in datasets_train:
        train_indices = list(range(len(datasets_train[node])))
        val_indices = list(range(len(datasets_val[node])))
        dataloaders_train[node] = DataLoader(
            datasets_train[node],
            batch_size=batch_size,
            sampler=OrderedSampler(train_indices, num_workers),
            num_workers=num_workers,
            drop_last=True,
            worker_init_fn=worker_init_fn
        )
        dataloaders_val[node] = DataLoader(
            datasets_val[node],
            batch_size=batch_size,
            sampler=OrderedSampler(val_indices, num_workers),
            num_workers=num_workers,
            drop_last=True,
            worker_init_fn=worker_init_fn
        )

    # Model, optimizer, and scheduler
    model = MHDNet(sub_networks, node_mapping, in_nodes, out_nodes, num_dimensions).to(device)
    optimizer = optim.Adam(model.parameters(), lr=learning_rate, weight_decay=1e-5)
    scheduler = WarmupCosineAnnealingLR(optimizer, warmup_epochs=warmup_epochs, T_max=num_epochs, eta_min=1e-6)

    # Save initial ONNX model before training starts
    model.eval()
    input_shapes = [(batch_size, *sub_networks[sub_net_name].node_configs[sub_node_id])
                    for global_node in in_nodes
                    for g_node, sub_net_name, sub_node_id in node_mapping
                    if g_node == global_node]
    inputs = [torch.randn(*shape).to(device) for shape in input_shapes]
    dynamic_axes = {
        **{f"input_{node}": {0: "batch_size"} for node in in_nodes},
        **{f"output_{node}": {0: "batch_size"} for node in out_nodes},
    }
    onnx_save_path = os.path.join(save_dir, "model_config_initial.onnx")
    torch.onnx.export(
        model,
        inputs,
        onnx_save_path,
        input_names=[f"input_{node}" for node in in_nodes],
        output_names=[f"output_{node}" for node in out_nodes],
        dynamic_axes=dynamic_axes,
        opset_version=13,
    )
    logger.info(f"Initial ONNX model saved to {onnx_save_path}")

    # Early stopping
    best_val_loss = float("inf")
    epochs_no_improve = 0
    log = {"epochs": []}

    for epoch in range(num_epochs):
        # Generate unique batch seeds for each epoch and worker
        epoch_seed = seed + epoch
        np.random.seed(epoch_seed)
        batch_seeds = np.random.randint(0, 1000000, size=len(dataloaders_train[node]))
        logger.info(f"Epoch {epoch + 1}: Generated {len(batch_seeds)} batch seeds")

        for batch_idx in range(len(dataloaders_train[node])):
            # Assign unique seed for each batch
            batch_seed = int(batch_seeds[batch_idx])
            logger.debug(f"Batch {batch_idx}, Seed {batch_seed}")
            for node in datasets_train:
                datasets_train[node].set_batch_seed(batch_seed)
            for node in datasets_val:
                datasets_val[node].set_batch_seed(batch_seed)

        train_loss, train_task_losses, train_metrics = train(
            model, dataloaders_train, optimizer, task_configs, out_nodes, epoch, num_epochs, sub_networks, node_mapping, node_transforms["train"]
        )

        epoch_log = {"epoch": epoch + 1, "train_loss": train_loss, "train_task_losses": train_task_losses, "train_metrics": train_metrics}

        if (epoch + 1) % validation_interval == 0:
            val_loss, val_task_losses, val_metrics = validate(
                model, dataloaders_val, task_configs, out_nodes, epoch, num_epochs, sub_networks, node_mapping
            )

            epoch_log.update({"val_loss": val_loss, "val_task_losses": val_task_losses, "metrics": val_metrics})

            if val_loss < best_val_loss:
                best_val_loss = val_loss
                epochs_no_improve = 0
                save_path = os.path.join(save_dir, "model_best.pth")
                torch.save(model.state_dict(), save_path)
                logger.info(f"Model saved to {save_path}")
            else:
                epochs_no_improve += validation_interval
                if epochs_no_improve >= patience:
                    logger.info(f"Early stopping at epoch {epoch + 1}")
                    break

        scheduler.step()
        log["epochs"].append(epoch_log)

    log_save_path = os.path.join(save_dir, "training_log.json")
    with open(log_save_path, "w") as f:
        json.dump(log, f, indent=4)
    logger.info(f"Training log saved to {log_save_path}")

if __name__ == "__main__":
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    main()
