import transformers
import torch
import os
import json
import random
import numpy as np
import argparse
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime
from tqdm import tqdm
from torch.nn import DataParallel
import logging
from transformers.modeling_gpt2 import GPT2Config, GPT2LMHeadModel
from transformers import BertTokenizer
from os.path import join, exists
from itertools import zip_longest, chain
from dataset import MyDataset
from torch.utils.data import Dataset, DataLoader
from torch.nn import CrossEntropyLoss
from sklearn.model_selection import train_test_split
from train import create_model
import torch.nn.functional as F

from rouge_chinese import Rouge

PAD = '[PAD]'
pad_id = 0


def set_interact_args():
    """
    Sets up the training arguments.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default='0,1', type=str, required=False, help='生成设备')
    parser.add_argument('--temperature', default=1, type=float, required=False, help='生成的temperature')
    parser.add_argument('--topk', default=8, type=int, required=False, help='最高k选1')
    parser.add_argument('--topp', default=0, type=float, required=False, help='最高积累概率')
    parser.add_argument('--model_config', default='summary_model/config.json', type=str, required=False,
                        help='模型参数')
    parser.add_argument('--train_raw_path', default='data/nlpcc2017_clean.json', type=str, required=False,
                        help='原始训练语料')
    parser.add_argument('--log_path', default='data/interacting.log', type=str, required=False, help='interact日志存放位置')
    parser.add_argument('--voca_path', default='vocabulary/vocab_small.txt', type=str, required=False, help='选择词库')
    parser.add_argument('--dialogue_model_path', default='summary_model/model_epoch10/', type=str, required=False,
                        help='对话模型路径')
    parser.add_argument('--pretrained_model', default='../gpt2-chinese-cluecorpussmall', type=str, required=False,
                        help='对话模型输出路径')
    parser.add_argument('--writer_dir', default='tensorboard_rouge/', type=str, required=False, help='Tensorboard路径')
    parser.add_argument('--save_samples_path', default="sample/", type=str, required=False, help="保存聊天记录的文件路径")
    parser.add_argument('--repetition_penalty', default=1.0, type=float, required=False,
                        help="重复惩罚参数，若生成的对话重复性较高，可适当提高该参数")
    parser.add_argument('--seed', type=int, default=None, help='设置种子用于生成随机数，以使得训练的结果是确定的')
    parser.add_argument('--max_len', type=int, default=300, help='每个utterance的最大长度,超过指定长度则进行截断')
    parser.add_argument('--max_history_len', type=int, default=1, help="dialogue history的最大长度")
    parser.add_argument('--no_cuda', default=False, help='不使用GPU进行预测')
    return parser.parse_args()


def create_logger(args):
    """
    将日志输出到日志文件和控制台
    """
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s')

    # 创建一个handler，用于写入日志文件
    file_handler = logging.FileHandler(
        filename=args.log_path)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    # 创建一个handler，用于将日志输出到控制台
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    logger.addHandler(console)

    return logger


def top_k_top_p_filtering(logits, top_k=0, top_p=0.0, filter_value=-float('Inf')):
    """ Filter a distribution of logits using top-k and/or nucleus (top-p) filtering
        Args:
            logits: logits distribution shape (vocabulary size)
            top_k > 0: keep only top k tokens with highest probability (top-k filtering).
            top_p > 0.0: keep the top tokens with cumulative probability >= top_p (nucleus filtering).
                Nucleus filtering is described in Holtzman et al. (http://arxiv.org/abs/1904.09751)
        From: https://gist.github.com/thomwolf/1a5a29f6962089e871b94cbd09daf317
    """
    assert logits.dim() == 1  # batch size 1 for now - could be updated for more but the code would be less clear
    top_k = min(top_k, logits.size(-1))  # Safety check
    if top_k > 0:
        # Remove all tokens with a probability less than the last token of the top-k
        # torch.topk()返回最后一维最大的top_k个元素，返回值为二维(values,indices)
        # ...表示其他维度由计算机自行推断
        indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
        logits[indices_to_remove] = filter_value  # 对于topk之外的其他元素的logits值设为负无穷

    if top_p > 0.0:
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)  # 对logits进行递减排序
        cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probs > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0

        indices_to_remove = sorted_indices[sorted_indices_to_remove]
        logits[indices_to_remove] = filter_value
    return logits


def random_read(args):
    file = json.load(open(args.train_raw_path, 'r', encoding='utf-8'))
    data = file['data']
    num_lines = int(len(data) * 0.2)
    # 随机选择 num_lines 个索引
    random_indices = random.sample(range(len(data)), num_lines)

    selected_lines = []
    for index in random_indices:
        selected_lines.append(data[index])

    return selected_lines


def generate(text, tokenizer, args, model, device):
    try:
        if len(text) >= 1024 - args.max_len:
            text = text[:1024 - args.max_len]
        input_ids = [tokenizer.cls_token_id]  # 每个input以[CLS]为开头
        input_ids.extend(tokenizer.encode(text))
        input_ids.append(tokenizer.sep_token_id)
        curr_input_tensor = torch.tensor(input_ids).long().to(device)
        generated = []
        # 最多生成max_len个token
        for _ in range(args.max_len):
            outputs = model(input_ids=curr_input_tensor)
            next_token_logits = outputs[0][-1, :]
            # 对于已生成的结果generated中的每个token添加一个重复惩罚项，降低其生成概率
            for id in set(generated):
                next_token_logits[id] /= args.repetition_penalty
            next_token_logits = next_token_logits / args.temperature
            # 对于[UNK]的概率设为无穷小，也就是说模型的预测结果不可能是[UNK]这个token
            next_token_logits[tokenizer.convert_tokens_to_ids('[UNK]')] = -float('Inf')
            filtered_logits = top_k_top_p_filtering(next_token_logits, top_k=args.topk, top_p=args.topp)
            # torch.multinomial表示从候选集合中无放回地进行抽取num_samples个元素，权重越高，抽到的几率越高，返回元素的下
            next_token = torch.multinomial(F.softmax(filtered_logits, dim=-1), num_samples=1)
            if next_token == tokenizer.sep_token_id:  # 遇到[SEP]则表明response生成结束
                break
            generated.append(next_token.item())
            curr_input_tensor = torch.cat((curr_input_tensor, next_token), dim=0)

        text = tokenizer.convert_ids_to_tokens(generated)
    except KeyboardInterrupt:
        exit(-1)
    return text


def main():
    args = set_interact_args()
    logger = create_logger(args)
    tb_writer = SummaryWriter(log_dir=args.writer_dir)
    # 当用户使用GPU,并且GPU可用时
    args.cuda = torch.cuda.is_available() and not args.no_cuda
    # args.cuda = False
    device = 'cuda' if args.cuda else 'cpu'
    logger.info('using device:{}'.format(device))
    os.environ["CUDA_VISIBLE_DEVICES"] = args.device
    tokenizer = BertTokenizer.from_pretrained(args.pretrained_model)
    model = GPT2LMHeadModel.from_pretrained(args.dialogue_model_path)
    model.to(device)
    model.eval()

    print('***********************Summary model start************************')
    select_text = random_read(args)
    rouge_sum = 0
    for i, line in enumerate(tqdm(select_text)):
        pre = generate(line['content'], tokenizer, args, model, device)
        pre_str = ''.join(pre)
        if i <= 5:
            print("The text = {}\nThe real title = {}\nThe generate title = {}\n".format(line['content'], line['title'],
                                                                                         pre_str))
        title = tokenizer.tokenize(line['title'])
        hyp = ' '.join(pre)
        ref = ' '.join(title)
        scores = Rouge().get_scores(hyp, ref)[0]
        average = sum(scores[rouge]['f'] for rouge in ('rouge-1', 'rouge-2', 'rouge-l')) / 3
        rouge_sum += average
        tb_writer.add_scalar('avr', average, i)

    print('rouge_ave={}'.format(rouge_sum / len(select_text)))


if __name__ == '__main__':
    main()
