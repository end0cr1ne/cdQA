# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Finetuning the library models for question-answering on SQuAD (Bert, XLM, XLNet)."""

from __future__ import absolute_import, division, print_function

import argparse
import logging
import os
import random
import glob

import numpy as np
import torch
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

from tensorboardX import SummaryWriter

from pytorch_transformers import (WEIGHTS_NAME, BertConfig,
                                  BertForQuestionAnswering, BertTokenizer,
                                  XLMConfig, XLMForQuestionAnswering,
                                  XLMTokenizer, XLNetConfig,
                                  XLNetForQuestionAnswering,
                                  XLNetTokenizer)

from pytorch_transformers import AdamW, WarmupLinearSchedule

from cdqa.reader.utils_squad import (read_squad_examples, convert_examples_to_features,
                         RawResult, write_predictions,
                         RawResultExtended, write_predictions_extended)

# The follwing import is the official SQuAD evaluation script (2.0).
# You can remove it from the dependencies if you are using this script outside of the library
# We've added it here for automated tests (see examples/test_examples.py file)
from cdqa.reader.utils_squad_evaluate import EVAL_OPTS, main as evaluate_on_squad

from sklearn.base import BaseEstimator

logger = logging.getLogger(__name__)

ALL_MODELS = sum((tuple(conf.pretrained_config_archive_map.keys()) \
                  for conf in (BertConfig, XLNetConfig, XLMConfig)), ())

MODEL_CLASSES = {
    'bert': (BertConfig, BertForQuestionAnswering, BertTokenizer),
    'xlnet': (XLNetConfig, XLNetForQuestionAnswering, XLNetTokenizer),
    'xlm': (XLMConfig, XLMForQuestionAnswering, XLMTokenizer),
}

def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

def to_list(tensor):
    return tensor.detach().cpu().tolist()

def train(args, train_dataset, model, tokenizer):
    """ Train the model """
    if args.local_rank in [-1, 0]:
        tb_writer = SummaryWriter()

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = WarmupLinearSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=t_total)
    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank,
                                                          find_unused_parameters=True)

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d",
                   args.train_batch_size * args.gradient_accumulation_steps * (torch.distributed.get_world_size() if args.local_rank != -1 else 1))
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 0
    tr_loss, logging_loss = 0.0, 0.0
    model.zero_grad()
    train_iterator = trange(int(args.num_train_epochs), desc="Epoch", disable=args.local_rank not in [-1, 0])
    set_seed(args)  # Added here for reproductibility (even between python 2 and 3)
    for _ in train_iterator:
        epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0])
        for step, batch in enumerate(epoch_iterator):
            model.train()
            batch = tuple(t.to(args.device) for t in batch)
            inputs = {'input_ids':       batch[0],
                      'attention_mask':  batch[1], 
                      'token_type_ids':  None if args.model_type == 'xlm' else batch[2],  
                      'start_positions': batch[3], 
                      'end_positions':   batch[4]}
            if args.model_type in ['xlnet', 'xlm']:
                inputs.update({'cls_index': batch[5],
                               'p_mask':    batch[6]})
            outputs = model(**inputs)
            loss = outputs[0]  # model outputs are always tuple in pytorch-transformers (see doc)

            if args.n_gpu > 1:
                loss = loss.mean() # mean() to average on multi-gpu parallel (not distributed) training
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
                torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
            else:
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                scheduler.step()  # Update learning rate schedule
                optimizer.step()
                model.zero_grad()
                global_step += 1

                if args.local_rank in [-1, 0] and args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    # Log metrics
                    if args.local_rank == -1 and args.evaluate_during_training:  # Only evaluate when single GPU otherwise metrics may not average well
                        results = evaluate(args, model, tokenizer)
                        for key, value in results.items():
                            tb_writer.add_scalar('eval_{}'.format(key), value, global_step)
                    tb_writer.add_scalar('lr', scheduler.get_lr()[0], global_step)
                    tb_writer.add_scalar('loss', (tr_loss - logging_loss)/args.logging_steps, global_step)
                    logging_loss = tr_loss

                if args.local_rank in [-1, 0] and args.save_steps > 0 and global_step % args.save_steps == 0:
                    # Save model checkpoint
                    output_dir = os.path.join(args.output_dir, 'checkpoint-{}'.format(global_step))
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = model.module if hasattr(model, 'module') else model  # Take care of distributed/parallel training
                    model_to_save.save_pretrained(output_dir)
                    torch.save(args, os.path.join(output_dir, 'training_args.bin'))
                    logger.info("Saving model checkpoint to %s", output_dir)

            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break
        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step


def evaluate(input_file, args, model, tokenizer, prefix=""):
    dataset, examples, features = load_and_cache_examples(input_file, args, tokenizer, evaluate=True, output_examples=True)

    if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
        os.makedirs(args.output_dir)

    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
    # Note that DistributedSampler samples randomly
    eval_sampler = SequentialSampler(dataset) if args.local_rank == -1 else DistributedSampler(dataset)
    eval_dataloader = DataLoader(dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

    # Eval!
    logger.info("***** Running evaluation {} *****".format(prefix))
    logger.info("  Num examples = %d", len(dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)
    all_results = []
    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        model.eval()
        batch = tuple(t.to(args.device) for t in batch)
        with torch.no_grad():
            inputs = {'input_ids':      batch[0],
                      'attention_mask': batch[1],
                      'token_type_ids': None if args.model_type == 'xlm' else batch[2]  # XLM don't use segment_ids
                      }
            example_indices = batch[3]
            if args.model_type in ['xlnet', 'xlm']:
                inputs.update({'cls_index': batch[4],
                               'p_mask':    batch[5]})
            outputs = model(**inputs)

        for i, example_index in enumerate(example_indices):
            eval_feature = features[example_index.item()]
            unique_id = int(eval_feature.unique_id)
            if args.model_type in ['xlnet', 'xlm']:
                # XLNet uses a more complex post-processing procedure
                result = RawResultExtended(unique_id            = unique_id,
                                           start_top_log_probs  = to_list(outputs[0][i]),
                                           start_top_index      = to_list(outputs[1][i]),
                                           end_top_log_probs    = to_list(outputs[2][i]),
                                           end_top_index        = to_list(outputs[3][i]),
                                           cls_logits           = to_list(outputs[4][i]))
            else:
                result = RawResult(unique_id    = unique_id,
                                   start_logits = to_list(outputs[0][i]),
                                   end_logits   = to_list(outputs[1][i]))
            all_results.append(result)
    
    # Compute predictions
    output_prediction_file = os.path.join(args.output_dir, "predictions_{}.json".format(prefix))
    output_nbest_file = os.path.join(args.output_dir, "nbest_predictions_{}.json".format(prefix))
    if args.version_2_with_negative:
        output_null_log_odds_file = os.path.join(args.output_dir, "null_odds_{}.json".format(prefix))
    else:
        output_null_log_odds_file = None

    if args.model_type in ['xlnet', 'xlm']:
        # XLNet uses a more complex post-processing procedure
        write_predictions_extended(examples, features, all_results, args.n_best_size,
                        args.max_answer_length, output_prediction_file,
                        output_nbest_file, output_null_log_odds_file, input_file,
                        model.config.start_n_top, model.config.end_n_top,
                        args.version_2_with_negative, tokenizer, args.verbose_logging)
    else:
        write_predictions(examples, features, all_results, args.n_best_size,
                        args.max_answer_length, args.do_lower_case, output_prediction_file,
                        output_nbest_file, output_null_log_odds_file, args.verbose_logging,
                        args.version_2_with_negative, args.null_score_diff_threshold)

    # Evaluate with the official SQuAD script
    evaluate_options = EVAL_OPTS(data_file=input_file,
                                 pred_file=output_prediction_file,
                                 na_prob_file=output_null_log_odds_file)
    results = evaluate_on_squad(evaluate_options)
    return results


def load_and_cache_examples(input_file, args, tokenizer, evaluate=False, output_examples=False):
    # Load data features from cache or dataset file
    cached_features_file = os.path.join(os.path.dirname(input_file) if isinstance(input_file, str) else '', 'cached_{}_{}_{}'.format(
    'dev' if evaluate else 'train',
    list(filter(None, args.model_name_or_path.split('/'))).pop(),
    str(args.max_seq_length)))
    if os.path.exists(cached_features_file) and not args.overwrite_cache and not output_examples:
        logger.info("Loading features from cached file %s", cached_features_file)
        features = torch.load(cached_features_file)
    else:
        logger.info("Creating features from dataset file at %s", input_file)
        examples = read_squad_examples(input_file=input_file,
                                       is_training=not evaluate,
                                       version_2_with_negative=args.version_2_with_negative)
        features = convert_examples_to_features(examples=examples,
                                                tokenizer=tokenizer,
                                                max_seq_length=args.max_seq_length,
                                                doc_stride=args.doc_stride,
                                                max_query_length=args.max_query_length,
                                                is_training=not evaluate)
        if args.local_rank in [-1, 0]:
            logger.info("Saving features into cached file %s", cached_features_file)
            torch.save(features, cached_features_file)

    # Convert to Tensors and build dataset
    all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
    all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
    all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
    all_cls_index = torch.tensor([f.cls_index for f in features], dtype=torch.long)
    all_p_mask = torch.tensor([f.p_mask for f in features], dtype=torch.float)
    if evaluate:
        all_example_index = torch.arange(all_input_ids.size(0), dtype=torch.long)
        dataset = TensorDataset(all_input_ids, all_input_mask, all_segment_ids,
                                all_example_index, all_cls_index, all_p_mask)
    else:
        all_start_positions = torch.tensor([f.start_position for f in features], dtype=torch.long)
        all_end_positions = torch.tensor([f.end_position for f in features], dtype=torch.long)
        dataset = TensorDataset(all_input_ids, all_input_mask, all_segment_ids,
                                all_start_positions, all_end_positions,
                                all_cls_index, all_p_mask)

    if output_examples:
        return dataset, examples, features
    return dataset


def predict(input_file, args, model, tokenizer, prefix=""):
    dataset, examples, features = load_and_cache_examples(input_file, args, tokenizer, evaluate=True, output_examples=True)

    if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
        os.makedirs(args.output_dir)

    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
    # Note that DistributedSampler samples randomly
    eval_sampler = SequentialSampler(dataset) if args.local_rank == -1 else DistributedSampler(dataset)
    eval_dataloader = DataLoader(dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

    # Eval!
    logger.info("***** Running evaluation {} *****".format(prefix))
    logger.info("  Num examples = %d", len(dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)
    all_results = []
    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        model.eval()
        batch = tuple(t.to(args.device) for t in batch)
        with torch.no_grad():
            inputs = {'input_ids':      batch[0],
                      'token_type_ids': None if args.model_type == 'xlm' else batch[1],  # XLM don't use segment_ids
                        'attention_mask': batch[2]}
            example_indices = batch[3]
            if args.model_type in ['xlnet', 'xlm']:
                inputs.update({'cls_index': batch[4],
                               'p_mask':    batch[5]})
            outputs = model(**inputs)

        for i, example_index in enumerate(example_indices):
            eval_feature = features[example_index.item()]
            unique_id = int(eval_feature.unique_id)
            if args.model_type in ['xlnet', 'xlm']:
                # XLNet uses a more complex post-processing procedure
                result = RawResultExtended(unique_id            = unique_id,
                                           start_top_log_probs  = to_list(outputs[0][i]),
                                           start_top_index      = to_list(outputs[1][i]),
                                           end_top_log_probs    = to_list(outputs[2][i]),
                                           end_top_index        = to_list(outputs[3][i]),
                                           cls_logits           = to_list(outputs[4][i]))
            else:
                result = RawResult(unique_id    = unique_id,
                                   start_logits = to_list(outputs[0][i]),
                                   end_logits   = to_list(outputs[1][i]))
            all_results.append(result)
    
    # Compute predictions
    output_prediction_file = os.path.join(args.output_dir, "predictions_{}.json".format(prefix))
    output_nbest_file = os.path.join(args.output_dir, "nbest_predictions_{}.json".format(prefix))
    output_null_log_odds_file = os.path.join(args.output_dir, "null_odds_{}.json".format(prefix))
    
    if args.model_type in ['xlnet', 'xlm']:
        # XLNet uses a more complex post-processing procedure
        out_eval, final_prediction = write_predictions_extended(examples, features, all_results, args.n_best_size,
                        args.max_answer_length, output_prediction_file,
                        output_nbest_file, output_null_log_odds_file, input_file,
                        model.config.start_n_top, model.config.end_n_top,
                        args.version_2_with_negative, tokenizer, args.verbose_logging)
    else:
       final_prediction = write_predictions(examples, features, all_results, args.n_best_size,
                        args.max_answer_length, args.do_lower_case, output_prediction_file,
                        output_nbest_file, output_null_log_odds_file, args.verbose_logging,
                        args.version_2_with_negative, args.null_score_diff_threshold)

    return final_prediction

class Reader(BaseEstimator):
    """
    """

    def __init__(self,
                 model_type=None,
                 model_name_or_path=None,
                 output_dir=None,
                 config_name="",
                 tokenizer_name="",
                 cache_dir="",
                 version_2_with_negative=True,
                 null_score_diff_threshold=0.0,
                 max_seq_length=384,
                 doc_stride=128,
                 max_query_length=64,
                 evaluate_during_training=True,
                 do_lower_case=True,
                 per_gpu_train_batch_size=8,
                 per_gpu_eval_batch_size=8,
                 learning_rate=5e-5,
                 gradient_accumulation_steps=1,
                 weight_decay=0.0,
                 adam_epsilon=1e-8,
                 max_grad_norm=1.0,
                 num_train_epochs=3.0,
                 max_steps=-1,
                 warmup_steps=0,
                 n_best_size=20,
                 max_answer_length=30,
                 verbose_logging=True,
                 logging_steps=50,
                 save_steps=50,
                 eval_all_checkpoints=True,
                 no_cuda=True,
                 overwrite_output_dir=True,
                 overwrite_cache=True,
                 seed=42,
                 local_rank=-1,
                 fp16=True,
                 fp16_opt_level='O1',
                 server_ip='',
                 server_port='',
                 pretrained_model_path=None):

            self.model_type = model_type
            self.model_name_or_path = model_name_or_path
            self.output_dir = output_dir
            self.config_name = config_name
            self.tokenizer_name = tokenizer_name
            self.cache_dir = cache_dir
            self.version_2_with_negative = version_2_with_negative
            self.null_score_diff_threshold = null_score_diff_threshold
            self.max_seq_length = max_seq_length
            self.doc_stride = doc_stride
            self.max_query_length = max_query_length
            self.evaluate_during_training = evaluate_during_training
            self.do_lower_case = do_lower_case
            self.per_gpu_train_batch_size = per_gpu_train_batch_size
            self.per_gpu_eval_batch_size = per_gpu_eval_batch_size
            self.learning_rate = learning_rate
            self.gradient_accumulation_steps = gradient_accumulation_steps
            self.weight_decay = weight_decay
            self.adam_epsilon = adam_epsilon
            self.max_grad_norm = max_grad_norm
            self.num_train_epochs = num_train_epochs
            self.max_steps = max_steps
            self.warmup_steps = warmup_steps
            self.n_best_size = n_best_size
            self.max_answer_length = max_answer_length
            self.verbose_logging = verbose_logging
            self.logging_steps = logging_steps
            self.save_steps = save_steps
            self.eval_all_checkpoints = eval_all_checkpoints
            self.no_cuda = no_cuda
            self.overwrite_output_dir = overwrite_output_dir
            self.overwrite_cache = overwrite_cache
            self.seed = seed
            self.local_rank = local_rank
            self.fp16 = fp16
            self.fp16_opt_level = fp16_opt_level
            self.server_ip = server_ip
            self.server_port = server_port
            self.pretrained_model_path = pretrained_model_path

            # Setup distant debugging if needed
            if self.server_ip and self.server_port:
                # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
                import ptvsd
                print("Waiting for debugger attach")
                ptvsd.enable_attach(address=(self.server_ip, self.server_port), redirect_output=True)
                ptvsd.wait_for_attach()

            # Setup CUDA, GPU & distributed training
            if self.local_rank == -1 or self.no_cuda:
                device = torch.device("cuda" if torch.cuda.is_available() and not self.no_cuda else "cpu")
                self.n_gpu = torch.cuda.device_count()
            else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
                torch.cuda.set_device(self.local_rank)
                device = torch.device("cuda", self.local_rank)
                torch.distributed.init_process_group(backend='nccl')
                self.n_gpu = 1
            self.device = device

            # Setup logging
            logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                                datefmt = '%m/%d/%Y %H:%M:%S',
                                level = logging.INFO if self.local_rank in [-1, 0] else logging.WARN)
            logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
                            self.local_rank, device, self.n_gpu, bool(self.local_rank != -1), self.fp16)

            # Set seed
            set_seed(self)

            # Load pretrained model and tokenizer
            if self.local_rank not in [-1, 0]:
                torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

            self.model_type = self.model_type.lower()
            config_class, self.model_class, tokenizer_class = MODEL_CLASSES[self.model_type]
            config = config_class.from_pretrained(self.config_name if self.config_name else self.model_name_or_path)
            self.tokenizer = tokenizer_class.from_pretrained(self.tokenizer_name if self.tokenizer_name else self.model_name_or_path, do_lower_case=self.do_lower_case)
            self.model = self.model_class.from_pretrained(self.model_name_or_path, from_tf=bool('.ckpt' in self.model_name_or_path), config=config)

            if self.local_rank == 0:
                torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

            self.model.to(self.device)

            logger.info("Training/evaluation parameters %s", self)

            if self.pretrained_model_path:
                # Load a trained model and vocabulary that you have fine-tuned
                self.model = self.model_class.from_pretrained(self.pretrained_model_path)
                # self.tokenizer = tokenizer_class.from_pretrained(self.pretrained_model_path)
                self.model.to(self.device)

    def fit(self, X, y=None):

        if os.path.exists(self.output_dir) and os.listdir(self.output_dir) and not self.overwrite_output_dir:
            raise ValueError("Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(self.output_dir))

        train_dataset = load_and_cache_examples(input_file=X, args=self, tokenizer=self.tokenizer, evaluate=False, output_examples=False)
        global_step, tr_loss = train(self, train_dataset, self.model, self.tokenizer)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)

        # Save the trained model and the tokenizer
        if self.local_rank == -1 or torch.distributed.get_rank() == 0:
            # Create output directory if needed
            if not os.path.exists(self.output_dir) and self.local_rank in [-1, 0]:
                os.makedirs(self.output_dir)

            logger.info("Saving model checkpoint to %s", self.output_dir)
            # Save a trained model, configuration and tokenizer using `save_pretrained()`.
            # They can then be reloaded using `from_pretrained()`
            model_to_save = self.model.module if hasattr(self.model, 'module') else self.model  # Take care of distributed/parallel training
            model_to_save.save_pretrained(self.output_dir)
            self.tokenizer.save_pretrained(self.output_dir)

            # Good practice: save your training arguments together with the trained model
            torch.save(self.get_params(), os.path.join(self.output_dir, 'training_args.bin'))

        return self

    def evaluate(self, X):

        # Evaluation - we can ask to evaluate all the checkpoints (sub-directories) in a directory
        results = {}
        if self.local_rank in [-1, 0]:
            checkpoints = [self.output_dir]
            if self.eval_all_checkpoints:
                checkpoints = list(os.path.dirname(c) for c in sorted(glob.glob(self.output_dir + '/**/' + WEIGHTS_NAME, recursive=True)))
                logging.getLogger("pytorch_transformers.modeling_utils").setLevel(logging.WARN)  # Reduce model loading logs
            
            logger.info("Evaluate the following checkpoints: %s", checkpoints)
            
            for checkpoint in checkpoints:
                # Reload the model
                global_step = checkpoint.split('-')[-1] if len(checkpoints) > 1 else ""
                self.model = self.model_class.from_pretrained(checkpoint)
                self.model.to(self.device)

                # Evaluate
                result = evaluate(input_file=X, args=self, model=self.model, tokenizer=self.tokenizer, prefix=global_step)
                
                result = dict((k + ('_{}'.format(global_step) if global_step else ''), v) for k, v in result.items())
                results.update(result)

        logger.info("Results: {}".format(results))

        return results

    def predict(self, X):

        out_eval, final_prediction = predict(input_file=X, args=self, model=self.model, tokenizer=self.tokenizer, prefix="")

        return out_eval, final_prediction
