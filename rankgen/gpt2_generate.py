import argparse
import glob
import numpy as np
import tqdm
import json
import torch
import os
import random

from transformers import GPT2Tokenizer, GPT2LMHeadModel
from utils import execute_gpt2, cudafy_tokens, form_partitions, truncate

parser = argparse.ArgumentParser()
parser.add_argument('--dataset', default="data/t5_xl_all_domains_wiki_random.jsonl")
parser.add_argument('--output_file', default="data/wiki_gpt2_medium_p90_multi.tsv")
parser.add_argument('--model_size', default="medium")
parser.add_argument('--num_instances', default=7713, type=int)
parser.add_argument('--num_samples', default=1, type=int)
parser.add_argument('--max_new_tokens', default=115, type=int)
parser.add_argument('--top_k', default=None, type=int)
parser.add_argument('--top_p', default=None, type=float)
parser.add_argument('--typical_p', default=None, type=float)
parser.add_argument('--truncate_fraction', default=0.0, type=float)
parser.add_argument('--num_shards', default=1, type=int)
parser.add_argument('--local_rank', default=0, type=int)
args = parser.parse_args()

with open(args.dataset, "r") as f:
    data = [json.loads(x) for x in f.read().strip().split("\n")]

tokenizer = GPT2Tokenizer.from_pretrained(f"gpt2-{args.model_size}")
tokenizer.pad_token = tokenizer.eos_token
model = GPT2LMHeadModel.from_pretrained(f"gpt2-{args.model_size}")
model.cuda()
model.eval()

avg_score = []
all_score = []
random.seed(484)
random.shuffle(data)
random.seed(None)
device = "cuda" if torch.cuda.is_available() else "cpu"


output = []
suffix_lens = []
gen_lens = []

def postprocess(outputs):
    return tokenizer.batch_decode(outputs, skip_special_tokens=True)

def truncate(text):
    """Truncate text to the last full sentence."""
    last_punc = 0
    if "." in text:
        last_punc = max(last_punc, text.rindex("."))
    if "?" in text:
        last_punc = max(last_punc, text.rindex("?"))
    if "!" in text:
        last_punc = max(last_punc, text.rindex("!"))
    if last_punc == 0:
      return text
    if last_punc != 0:
        text = text[:last_punc + 1]
    return text

if args.num_shards > 1:
    partitions = form_partitions(data, args.num_shards)
    data = partitions[args.local_rank]
    args.output_file = f'{args.output_file}.shard_{args.local_rank}'

for idx, dd in tqdm.tqdm(enumerate(data), total=min(len(data), args.num_instances)):
    if len(suffix_lens) >= args.num_instances:
        break
    prefix = dd['prefix']
    ground_truth = dd["targets"][0]
    batch = tokenizer(prefix, truncation=True, padding="longest", return_tensors="pt", max_length=1024 - args.max_new_tokens).to(device)
    num_tokens = len(batch['input_ids'][0])
    if num_tokens >= 1024 - args.max_new_tokens - 3:
        print("long sequence detected")
    with torch.no_grad():
        generation = model.generate(**batch,
            do_sample=True,
            output_scores=True,
            return_dict_in_generate=True,
            max_new_tokens=args.max_new_tokens,
            top_k=args.top_k,
            typical_p=args.typical_p,
            top_p=args.top_p,
            num_return_sequences=args.num_samples)
        gen_text = postprocess(generation['sequences'][:, num_tokens:])
        gen_text = [" ".join(x.split()) for x in gen_text]
        gen_text = [truncate(x) for x in gen_text]

    # for i in range(len(gen_text)):
    #     if random.random() < args.truncate_fraction:
    #         gen_text[i] = truncate(gen_text[i][:-1])

    if "suffix" in dd:
        suffix_str = dd['suffix']
    else:
        suffix_str = dd['targets'][0]

    suffix_lens.append(len(suffix_str.split()))
    for x in gen_text:
        gen_lens.append(len(x.split()))
    # output += f"{prefix}\t{suffix_str}\tplaceholder\tplaceholder\n"

    curr_output = {}
    curr_output["prefix"] = f"{prefix}"
    curr_output["targets"] = [ground_truth]
    
    for x in gen_text:
      curr_output["targets"].append(f"{x}")

    output.append(json.dumps(curr_output))

    # for x in gen_text:
    #     # output += f"{prefix}\t{x}\tplaceholder\tplaceholder\n"
    #     output.append(json.dumps({
    #     "prefix": f"{prefix}",
    #     "target": f"{x}"
    # }))

    if (idx + 1) % 100 == 0:
        print(f"Avg suffix length = {np.mean(suffix_lens):.4f} ({len(suffix_lens)} samples), avg gen length = {np.mean(gen_lens):.4f} ({len(gen_lens)} samples)")
        with open(args.output_file, "w") as f:
            f.write("\n".join(output) + "\n")

with open(args.output_file, "w") as f:
    f.write("\n".join(output) + "\n")
