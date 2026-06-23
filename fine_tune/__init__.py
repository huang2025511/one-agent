"""模型微调支持模块 — 支持 LLM 模型的参数高效微调。

提供：
  - LoRA (Low-Rank Adaptation) 微调支持
  - QLoRA 量化微调
  - 数据集准备和处理
  - 训练进度追踪
  - 模型导出和部署
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class FineTuneConfig:
    """微调配置类。"""
    model_name: str = "base-model"
    dataset_path: str = ""
    output_dir: str = "data/fine_tune"
    lora_rank: int = 8
    lora_alpha: int = 16
    batch_size: int = 8
    epochs: int = 3
    learning_rate: float = 2e-4
    weight_decay: float = 0.01
    max_seq_length: int = 512
    quantize_4bit: bool = False
    quantize_8bit: bool = False
    logging_steps: int = 10
    save_steps: int = 100
    eval_steps: int = 50


@dataclass
class TrainingProgress:
    """训练进度跟踪类。"""
    epoch: int = 0
    step: int = 0
    loss: float = 0.0
    eval_loss: float = 0.0
    accuracy: float = 0.0
    learning_rate: float = 0.0
    eta: float = 0.0
    total_steps: int = 0
    status: str = "pending"  # pending / running / completed / failed
    error: str = ""


@dataclass
class FineTuneResult:
    """微调结果类。"""
    model_path: str = ""
    adapter_path: str = ""
    metrics: Dict[str, float] = field(default_factory=dict)
    training_time: float = 0.0
    error: str = ""
    success: bool = False


class FineTuneManager(Plugin):
    """模型微调管理器 — 支持 LoRA/QLoRA 微调。"""

    name = "fine_tune"

    def __init__(self):
        super().__init__()
        self._config = FineTuneConfig()
        self._progress = TrainingProgress()
        self._training_task = None
        self._running = False
        self._client: Optional[httpx.AsyncClient] = None

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("fine_tune", {}) or {}
        self._config = FineTuneConfig(
            model_name=cfg.get("model_name", "base-model"),
            output_dir=cfg.get("output_dir", "data/fine_tune"),
            lora_rank=cfg.get("lora_rank", 8),
            lora_alpha=cfg.get("lora_alpha", 16),
            batch_size=cfg.get("batch_size", 8),
            epochs=cfg.get("epochs", 3),
            learning_rate=float(cfg.get("learning_rate", 2e-4)),
            weight_decay=float(cfg.get("weight_decay", 0.01)),
            max_seq_length=cfg.get("max_seq_length", 512),
            quantize_4bit=cfg.get("quantize_4bit", False),
            quantize_8bit=cfg.get("quantize_8bit", False),
        )
        Path(self._config.output_dir).mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(timeout=60)
        logger.info("Fine tune manager configured")

    async def stop(self) -> None:
        if self._training_task:
            self._training_task.cancel()
            try:
                await self._training_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        await super().stop()

    def prepare_dataset(self, data: List[Dict[str, str]], output_path: str) -> bool:
        """准备微调数据集。
        
        Args:
            data: 数据列表，每个元素包含 "instruction" 和 "response"
            output_path: 输出文件路径
        
        Returns:
            是否成功
        """
        try:
            formatted_data = []
            for item in data:
                formatted_data.append({
                    "instruction": item.get("instruction", ""),
                    "response": item.get("response", ""),
                    "input": item.get("input", ""),
                })
            
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(formatted_data, f, indent=2, ensure_ascii=False)
            
            logger.info("Dataset prepared: %s (%d samples)", output_path, len(formatted_data))
            return True
        except Exception as exc:
            logger.error("Failed to prepare dataset: %s", exc)
            return False

    async def start_fine_tune(self, config: FineTuneConfig = None) -> str:
        """启动微调任务。
        
        Args:
            config: 微调配置（可选，使用默认配置）
        
        Returns:
            任务ID
        """
        if config:
            self._config = config
        
        if not self._config.dataset_path or not os.path.exists(self._config.dataset_path):
            raise ValueError("Dataset path is required")
        
        task_id = f"ft_{int(time.time())}"
        self._progress = TrainingProgress(
            status="running",
            total_steps=self._config.epochs * 100  # 估算总步数
        )
        
        self._training_task = asyncio.create_task(self._run_training(task_id))
        return task_id

    async def _run_training(self, task_id: str) -> None:
        """执行微调训练。"""
        try:
            # 创建训练脚本
            script_path = Path(self._config.output_dir) / f"train_{task_id}.py"
            script_content = self._generate_training_script(task_id)
            script_path.write_text(script_content, encoding='utf-8')
            
            # 运行训练
            process = await asyncio.create_subprocess_exec(
                "python", str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._config.output_dir
            )
            
            # 实时读取输出
            async def read_stream(stream):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    self._parse_training_output(line.decode())
            
            await asyncio.gather(
                read_stream(process.stdout),
                read_stream(process.stderr)
            )
            
            await process.wait()
            
            if process.returncode == 0:
                self._progress.status = "completed"
                logger.info("Fine tune completed successfully")
            else:
                self._progress.status = "failed"
                self._progress.error = f"Training failed with code {process.returncode}"
                logger.error("Fine tune failed: %s", self._progress.error)
                
        except Exception as exc:
            self._progress.status = "failed"
            self._progress.error = str(exc)
            logger.error("Training error: %s", exc)

    def _generate_training_script(self, task_id: str) -> str:
        """生成训练脚本内容。"""
        script = f"""
import os
os.environ['TRANSFORMERS_CACHE'] = '{os.path.join(self._config.output_dir, "cache")}'

from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainingArguments,
    Trainer,
    DataCollatorForLanguageModeling,
)
from peft import LoraConfig, get_peft_model
import json

# 加载模型
model_name = "{self._config.model_name}"
tokenizer = AutoTokenizer.from_pretrained(model_name)
tokenizer.pad_token = tokenizer.eos_token

model = AutoModelForCausalLM.from_pretrained(
    model_name,
    load_in_4bit={self._config.quantize_4bit},
    load_in_8bit={self._config.quantize_8bit},
    device_map="auto",
)

# 配置 LoRA
lora_config = LoraConfig(
    r={self._config.lora_rank},
    lora_alpha={self._config.lora_alpha},
    target_modules=["q_proj", "v_proj"],
    lora_dropout=0.05,
    bias="none",
    task_type="CAUSAL_LM",
)
model = get_peft_model(model, lora_config)
model.print_trainable_parameters()

# 加载数据集
with open("{self._config.dataset_path}", "r", encoding="utf-8") as f:
    data = json.load(f)

def tokenize_function(examples):
    return tokenizer(
        examples["instruction"] + " " + examples.get("input", ""),
        examples["response"],
        max_length={self._config.max_seq_length},
        truncation=True,
    )

# 简单的数据处理
tokenized_data = [tokenize_function(d) for d in data]

# 训练参数
training_args = TrainingArguments(
    output_dir="{os.path.join(self._config.output_dir, task_id)}",
    per_device_train_batch_size={self._config.batch_size},
    num_train_epochs={self._config.epochs},
    learning_rate={self._config.learning_rate},
    weight_decay={self._config.weight_decay},
    logging_steps={self._config.logging_steps},
    save_steps={self._config.save_steps},
    evaluation_strategy="steps",
    eval_steps={self._config.eval_steps},
    report_to="none",
)

# 训练
trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tokenized_data,
    data_collator=DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False),
)

trainer.train()

# 保存模型
model.save_pretrained("{os.path.join(self._config.output_dir, task_id, "adapter")}")
print("Training complete!")
"""
        return script

    def _parse_training_output(self, line: str) -> None:
        """解析训练输出更新进度。"""
        if "loss:" in line.lower():
            try:
                parts = line.split()
                for i, part in enumerate(parts):
                    if part == "loss:" or "loss" in part.lower():
                        self._progress.loss = float(parts[i + 1])
                        break
            except Exception:
                pass
        
        if "epoch" in line.lower():
            try:
                parts = line.split()
                for i, part in enumerate(parts):
                    if "epoch" in part.lower():
                        self._progress.epoch = int(float(parts[i + 1]))
                        break
            except Exception:
                pass

    def get_progress(self) -> TrainingProgress:
        """获取当前训练进度。"""
        return self._progress

    def list_fine_tunes(self) -> List[Dict[str, Any]]:
        """列出所有微调任务。"""
        results = []
        output_dir = Path(self._config.output_dir)
        if not output_dir.exists():
            return results
        
        for item in output_dir.iterdir():
            if item.is_dir() and item.name.startswith("ft_"):
                results.append({
                    "task_id": item.name,
                    "created_at": item.stat().st_ctime,
                    "path": str(item),
                })
        
        return sorted(results, key=lambda x: x["created_at"], reverse=True)

    def export_model(self, task_id: str, output_path: str) -> bool:
        """导出微调后的模型。"""
        try:
            src_dir = Path(self._config.output_dir) / task_id / "adapter"
            if not src_dir.exists():
                return False
            
            dest_dir = Path(output_path)
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            shutil.copytree(src_dir, dest_dir)
            
            logger.info("Model exported to %s", output_path)
            return True
        except Exception as exc:
            logger.error("Failed to export model: %s", exc)
            return False

    def cancel_training(self) -> bool:
        """取消正在进行的训练。"""
        if self._training_task:
            self._training_task.cancel()
            self._progress.status = "cancelled"
            return True
        return False

    def merge_adapter(self, base_model: str, adapter_path: str, output_path: str) -> bool:
        """合并 LoRA adapter 到基础模型。"""
        try:
            script = f"""
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

base_model = "{base_model}"
adapter_path = "{adapter_path}"
output_path = "{output_path}"

model = AutoModelForCausalLM.from_pretrained(base_model)
model = PeftModel.from_pretrained(model, adapter_path)
model = model.merge_and_unload()

tokenizer = AutoTokenizer.from_pretrained(base_model)

model.save_pretrained(output_path)
tokenizer.save_pretrained(output_path)
print("Model merged successfully!")
"""
            script_path = Path(self._config.output_dir) / "merge.py"
            script_path.write_text(script, encoding='utf-8')
            
            result = subprocess.run(
                ["python", str(script_path)],
                capture_output=True,
                text=True,
                cwd=self._config.output_dir
            )
            
            if result.returncode == 0:
                logger.info("Model merged successfully")
                return True
            else:
                logger.error("Merge failed: %s", result.stderr)
                return False
        except Exception as exc:
            logger.error("Merge error: %s", exc)
            return False