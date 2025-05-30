import os, sys
sys.path.append(os.getenv('LLAVA_HOME'))

import einops
from collections import defaultdict
import numpy as np
import torch
from torchvision.transforms.functional import to_pil_image
import gradio as gr
import PIL
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.colors import to_rgba

import seaborn
from PIL import Image, ImageDraw
import pandas as pd
from scipy import stats

import logging

logger = logging.getLogger(__name__)
# cmap = plt.get_cmap('jet')
cmap = plt.get_cmap('coolwarm')
separators_list = ['.',',','?','!', ':', ';', '</s>', '/', '!', '(', ')', '[', ']', '{', '}', '<', '>', '|', '\\', '-', '_', '+', '=', '*', '&', '^', '%', '$', '#', '@', '!', '~', '`', ' ', '\t', '\n', '\r', '\x0b', '\x0c']

def move_to_device(input, device='cpu'):

    if isinstance(input, torch.Tensor):
        return input.to(device).detach()
    elif isinstance(input, list):
        return [move_to_device(inp) for inp in input]
    elif isinstance(input, tuple):
        return tuple([move_to_device(inp) for inp in input])
    elif isinstance(input, dict):
        return dict( ((k, move_to_device(v)) for k,v in input.items()))
    else:
        raise ValueError(f"Unknown data type for {input.type}")

def convert_token2word(R_i_i, tokens, separators_list):
    current_count = 1
    current_rel_map = 0
    word_rel_maps = {}
    current_word = ""
    for token, rel in zip(tokens, R_i_i):
        if not token.startswith('▁') and token not in separators_list:
            current_word += token
            current_rel_map += rel
            current_count += 1
        else:
            # Otherwise, store the current word's relevancy map and start a new word
            word_rel_maps[current_word] = current_rel_map / current_count
            current_word = token
            current_rel_map = rel
            current_count = 1
    return list(word_rel_maps.keys()), torch.Tensor(list(word_rel_maps.values()))

def draw_heatmap_on_image(mat, img_recover, normalize=True):
    if normalize:
        mat = (mat - mat.min()) / (mat.max() - mat.min())
    mat = cmap(mat)  #.cpu().numpy())
    mat = Image.fromarray((mat[:, :, :3] * 255).astype(np.uint8)).resize((336,336), Image.BICUBIC)
    mat.putalpha(128)
    img_overlay_attn = img_recover.copy()
    img_overlay_attn.paste(mat, mask=mat)
    
    return img_overlay_attn

def attn_update_slider(state):
    fn_attention = state.attention_key + '_attn.pt'
    attentions = torch.load(fn_attention,weights_only=True, mmap=True)
    num_layers = len(attentions[0])
    # is slider the best module for this ? 
    return state, gr.Slider(0, num_layers-1, value=num_layers-1, step=1, label="Layer")


def handle_attentions_i2t(state, highlighted_text,token_idx=0):
    '''
        Draw attention heatmaps and return as a list of PIL images
        steps:
            * collect multihead attention for selected tokens
            mha = attn[selected_tokens][:]
            if num_query > 1 : # for token 0
                select qeury of last input id
            fetch img_attn
            img_attn = mha[img_idx:img_idx+576]
            
            for layer: for head:
            cummuatively add img_attn over all heads in a layer
            average the attn over number of selected tokens

            sort on highest response

    '''

    if not hasattr(state, 'attention_key'):
        return None, None, [], None
    fn_attention = state.attention_key + '_attn.pt'
    recovered_image = state.recovered_image
    img_idx = state.image_idx
    logger.info(f"image idx: {img_idx}") # 5?
    
    # which tokens to backprop from -> token_idx_list
    if highlighted_text is not None:
        generated_text = state.output_ids_decoded
        token_idx_map = dict((t,i) for i,t in enumerate(generated_text))
        token_idx_list = []
        for item in highlighted_text:
            label = item['class_or_confidence']
            if label is None:
                continue
            tokens = item['token'].split(' ')

            for tok in tokens:
                tok = tok.strip(' ')
                if tok in token_idx_map:
                    token_idx_list.append(token_idx_map[tok])
                else:
                    logger.warning(f'{tok} not found in generated text')

        if not token_idx_list:
            logger.info(highlighted_text)
            logger.info(generated_text)
            gr.Error(f"Selected text not found in generated output")
            return None, None, [], None
        
        generated_text = []
        for data in highlighted_text:
            generated_text.extend([(data['token'], None if data['class_or_confidence'] is None else "'"), (' ', None)])
    else:
        token_idx_list = [0]
        generated_text = []
        for text in state.output_ids_decoded:
            generated_text.extend([(text, None), (' ', None)])
        

    if not os.path.exists(fn_attention):
        gr.Error('Attention file not found. Please re-run query.')
    else:
        attentions = torch.load(fn_attention,weights_only=True)
        logger.info(f'Loaded attention from {fn_attention}')
        if len(attentions) == len(state.output_ids_decoded):
            gr.Error('Mismatch between lengths of attentions and output tokens')
        batch_size, num_heads, inp_seq_len, seq_len = attentions[0][0].shape
        num_layers = len(attentions[0])
        # cmap = plt.get_cmap('jet')
        cmap = plt.get_cmap('coolwarm')
        
        img_attn_layers = dict()
        img_attn_mean_layers = dict()
        for layer_idx in range(num_layers):
            img_attn_list = []
            img_attn_mean = []
            for head_idx in range(num_heads):
                img_attn = None
                for token_idx in token_idx_list:
                    if token_idx >= len(attentions):
                        logger.info(f'token index {token_idx} out of bounds')
                        continue
                    mh_attention = attentions[token_idx][layer_idx]
                    batch_size, num_heads, inp_seq_len, seq_len = mh_attention.shape
                    if inp_seq_len > 1:
                        # if >1 that is attention of output token =0
                        # so we select the query of the last input id token.
                        mh_attention = mh_attention[:,:,-1,:]

                    # else : multihead attention of the output token >0
                    mh_attention = mh_attention.squeeze() # take out the batch dimension (here always 1)
                    img_attn_token = mh_attention[head_idx, img_idx:img_idx+576].reshape(24,24).float().cpu().numpy()

                    if img_attn is None:
                        img_attn = img_attn_token
                    else:
                        img_attn += img_attn_token
                img_attn /= len(token_idx_list)
                img_overlay_attn = draw_heatmap_on_image(img_attn, recovered_image)

                img_attn_list.append((img_overlay_attn, f'Layer_{layer_idx}_Head_{head_idx}'))

                # Calculate mean attention per head
                # img_attn = mh_attention[head_idx, img_idx:img_idx+576].reshape(24,24).cpu().numpy()

                img_attn /= img_attn.max()
                img_attn_mean.append(img_attn.mean())
            img_attn_list = [x for _, x in sorted(zip(img_attn_mean, img_attn_list), key=lambda pair: pair[0], reverse=True)]
            img_attn_layers[layer_idx] = img_attn_list
            img_attn_mean_layers[layer_idx] = img_attn_mean
            logger.info(f"Calculated for layer: {layer_idx}")


        fig, ax = plt.subplots(num_layers,1,figsize=(15,2*num_layers))
        fig.suptitle("Mean (per layer) scores for all layers ",fontsize=15,y=0,x=0)
        for layer_id in range(num_layers):
                seaborn.heatmap([img_attn_mean_layers[layer_id]], 
                    cmap="coolwarm",linewidths=.3,annot=True,
                    cbar_kws={"orientation": "vertical", "shrink":0.3},
                    ax=ax[layer_id]
                )
                ax[layer_id].set_ylabel(f'Layer "{layer_id}')
        fig.tight_layout()
        plt.savefig(state.attention_key + 'mean_per_layer_scores_for_all_layers.png')

        all_attn_list = [item for sublist in img_attn_layers.values() for item in sublist]
        logger.info(f"length attention images: {len(all_attn_list)}")
        logger.info(f"Mean Attention between the image and the token {[state.output_ids_decoded[tok] for tok in token_idx_list]}")

    return generated_text, recovered_image, all_attn_list[::-1], fig

def handle_relevancy(state, type_selector,incude_text_relevancy=False):
    incude_text_relevancy = True
    logger.debug(f'incude_text_relevancy: {incude_text_relevancy}')

    if not hasattr(state, 'attention_key'):
        return []
    
    fn_attention = state.attention_key + '_relevancy.pt'
    recovered_image = state.recovered_image
    img_idx = state.image_idx
    logger.info(f"Image Idx:{img_idx}")

    word_rel_maps = torch.load(fn_attention)
    if type_selector not in word_rel_maps:
        logger.warning(f'{type_selector} not in keys: {word_rel_maps.keys()}')
        return []

    word_rel_map = word_rel_maps[type_selector]
    image_list = []
    i = 0
    for rel_key, rel_map in word_rel_map.items():
        i+=1
        if rel_key in separators_list:
            continue
        if (rel_map.shape[-1] != 577) and img_idx:
            if not incude_text_relevancy:
                rel_map = rel_map[-1,:][img_idx:img_idx+576].reshape(24,24).float().cpu().numpy()
                normalize_image_tokens = True
            if incude_text_relevancy:
                input_text_tokenized = state.input_text_tokenized
                input_text_tokenized_len = int(len(input_text_tokenized))
                img_idx = int(img_idx)
                rel_maps_no_system = torch.cat((rel_map[-1,:][img_idx:img_idx+576], rel_map[-1,:][img_idx+576+3:576 + input_text_tokenized_len-1-5]))
                logger.debug(f'shape of rel_maps_no_system: {rel_maps_no_system.shape}')
                # make sure the sum of the relevancy scores is 1
                # if rel_maps_no_system.sum() != 0:
                #     rel_maps_no_system /= rel_maps_no_system.sum()
                rel_maps_no_system = (rel_maps_no_system - rel_maps_no_system.min()) / (rel_maps_no_system.max() - rel_maps_no_system.min())
                rel_map = rel_maps_no_system[:576].reshape(24,24).cpu().numpy()
                normalize_image_tokens = False
        else:
            rel_map = rel_map[0,1:].reshape(24,24).cpu().numpy()
            normalize_image_tokens = True
        rel_map = draw_heatmap_on_image(rel_map, recovered_image, normalize=normalize_image_tokens)
        # strip _ from all rel keys
        rel_key = rel_key.strip('▁').strip('_')
        image_list.append( (rel_map, rel_key))

    return image_list

def grid_size(len):
    n_columns = 3 if len < 16 else 4
    n_rows = int(np.ceil(len / n_columns))
    return (n_rows, n_columns)

def fig2img(fig):
    """Convert a Matplotlib figure to a PIL Image and return it"""
    import io
    buf = io.BytesIO()
    fig.savefig(buf)
    buf.seek(0)
    img = Image.open(buf)
    return img

def handle_text_relevancy(state, type_selector):
    if type_selector != "llama":
        return [], []
    else:
        tokens = state.output_ids_decoded
        fn_attention = state.attention_key + '_relevancy.pt'
        img_idx = state.image_idx
        input_text_tokenized = state.input_text_tokenized
        word_rel_maps = torch.load(fn_attention)
        
        input_text_tokenized_all = input_text_tokenized.copy()
        # loop over all output tokens
        word_rel_map = word_rel_maps["llama_token"]
        # grid_size_temp = grid_size(len(rel_scores))
        all_figs = []
        highlighted_tokens = []
        for word, rel_map in word_rel_map.items():
            if word in separators_list:
                continue
            fig, ax = plt.subplots(figsize=(5, 5))
            # if the token is not a separator
            # if i < len(tokens) and tokens[i] not in separators_list:
            img_avg_rel = rel_map[-1,:][img_idx:img_idx+576].mean()
            img_max_rel = rel_map[-1,:][img_idx:img_idx+576].max()
            logger.debug(f'img_avg_rel for token {word}: {img_avg_rel}')
            # exclude the image tokens from the rel_scores[i] and replace all of them by a single value of the average relevancy for the image
            current_relevency = rel_map[-1,:][:img_idx].clone()
            # add the average relevancy for the image to the current_relevency tensor
            current_relevency = torch.cat((current_relevency, img_avg_rel.unsqueeze(0)))    
            current_relevency = torch.cat((current_relevency, rel_map[-1,:][img_idx+576:576 + len(input_text_tokenized_all)-1]))
            current_relevency = current_relevency.cpu()
            logger.debug(f'shape of text relevancy map: {rel_map[-1,:].shape}')
            #rel_score_text = rel_scores[i][-1,:][:img_idx]
            assert len(current_relevency) == len(input_text_tokenized_all), f"The length of the relevancy score ({len(current_relevency)}) is not the same as the length of the input tokens ({len(input_text_tokenized_all)})\n{input_text_tokenized_all}"
            current_relevency = current_relevency[img_idx+3:-5]
            input_text_tokenized = input_text_tokenized_all[img_idx+3:-5]
            input_text_tokenized_word, current_relevency_word = convert_token2word(current_relevency, input_text_tokenized, separators_list) 

            current_relevency_word_topk = current_relevency_word.topk(min(3,len(word_rel_map)))
            max_rel_scores = current_relevency_word_topk.values
            max_rel_scores = torch.cat((max_rel_scores, img_max_rel.unsqueeze(0).cpu()))
            max_rel_scores_idx = current_relevency_word_topk.indices
            max_input_token = [input_text_tokenized_word[j].lstrip('▁').lstrip('_') for j in max_rel_scores_idx]

            # Image to text relevancy ratio
            # img_text_rel_ratio = max_rel_scores[-1] / current_relevency_word.mean()
            img_text_rel_value = stats.percentileofscore(max_rel_scores, img_max_rel.item(), kind='strict') / 100

            highlighted_tokens.extend(
                [
                    (word.strip('▁'), float(img_text_rel_value)),
                    (" ", None)
                ]
            )

            max_input_token.append("max_img")
            ax.bar(max_input_token, max_rel_scores)
            # ax.set_xticklabels(max_input_token, fontsize=12)

            # save the plot per each output token
            # make part of the title bold
            ax.set_title(f'Output Token: {word.strip("▁").strip("_")}', fontsize=15)
            # add labels for the x and y axis
            ax.set_xlabel('Input Tokens', fontsize=15)
            ax.set_ylabel('Relevancy Score', fontsize=15)

            fig.tight_layout()

            fig_pil = fig2img(fig)
            all_figs.append(fig_pil)

        return all_figs, highlighted_tokens

def handle_image_click(image,box_grid, x, y):
    # Calculate which box was clicked
    box_width = image.size[1] // 24
    box_height = image.size[0] // 24

    box_x = x // box_width
    box_y = y // box_height

    box_grid[box_x][box_y] = not box_grid[box_x][box_y]
    
    # Add a transparent teal box to the image at the clicked location
    overlay = image.copy()
    draw = ImageDraw.Draw(overlay)
    indices = np.where(box_grid)
    for i, j in zip(*indices):
        draw.rectangle([(i * box_width, j * box_height), ((i + 1) * box_width, (j + 1) * box_height)], fill=(255, 100, 100, 128))

    image = Image.blend(image, overlay, alpha=0.8)

    # Return the updated image
    return image, box_grid

def handle_box_reset(input_image,box_grid): 
    for i in range(24):
        for j in range(24):
            box_grid[i][j] = False
    try:
        to_return = input_image.copy()
    except:
        to_return = None
    return to_return, box_grid


def boxes_click_handler(image, box_grid, event: gr.SelectData):
    if event is not None:
        x, y = event.index[0], event.index[1]

        image,box_grid = handle_image_click(image,box_grid, x, y)
        if x is not None and y is not None:
            return image,box_grid

def plot_attention_analysis(state, attn_modality_select):
    """
    Img to response tokens
    steps:
        for each layer:
            collect last query for all tokens
            (token =0 we collect query from last patch)
            and (token>1 is q=1).The shape of the keys would be num_heads ,
            # here multihead attention from all the tokens would be of the shape:
            #+ (img_idx+img_patches+input_ids: ___ + (0.. total_out_tokens-1))
            for each head:
                stack keys only ranging the image patches
                store mean of keys across all tokens

    Question tokens to response tokens:
        similar to above :
            but here we skip image tokens and only collect :
            mh_attns[img_idx+576:img_idx+576+len(question_tokens)]
    """
    fn_attention = state.attention_key + '_attn.pt'
    recovered_image = state.recovered_image
    img_idx = state.image_idx
    logger.info(f"From Plot attention analysis {img_idx=}")

    if os.path.exists(fn_attention):
        attentions = torch.load(fn_attention,weights_only=True)
        logger.info(f'Loaded attention from {fn_attention}')
        if len(attentions) == len(state.output_ids_decoded):
            gr.Error('Mismatch between lengths of attentions and output tokens')
        
        num_tokens = len(attentions)
        num_layers = len(attentions[0])
        # first tokens last mh attention
        last_mh_attention = attentions[0][-1]
        batch_size, num_heads, inp_seq_len, seq_len = attentions[0][0].shape
        generated_text = state.output_ids_decoded
    
    else:
        return state, None
    
    # Img2TextAns Attention
    heatmap_mean = defaultdict(dict)
    raw_heatmap = defaultdict(dict)
    if attn_modality_select == "Image-to-Answer":
        for layer_idx in range(num_layers):
            mh_attentions = [attentions[i][layer_idx][:,:,-1,:].squeeze() for i in range(len(generated_text))]
            for head_idx in range(num_heads):
                # mh_attentions = []
                img_attn = torch.stack([mh_attention[head_idx, img_idx:img_idx+576].reshape(24,24) for mh_attention in mh_attentions]).float().cpu().numpy()
                # img_attn /= img_attn.max()
                heatmap_mean[layer_idx][head_idx] =  img_attn.mean() # img_attn.mean((1,2))
                raw_heatmap[layer_idx][head_idx] = img_attn.mean(axis=0) #only over tokens
    elif attn_modality_select == "Question-to-Answer":
        fn_input_ids = state.attention_key + '_input_ids.pt'
        img_idx = state.image_idx
        input_ids = torch.load(fn_input_ids,weights_only=True)
        len_question_only = input_ids.shape[1] - img_idx - 1
        for layer_idx in range(num_layers):
            for head_idx in range(num_heads):
                mh_attentions = []
                mh_attentions = [attentions[i][layer_idx][:,:,-1,:].squeeze() for i in range(len(generated_text))]
                ques_attn = torch.stack([mh_attention[head_idx, img_idx+576:img_idx+576+len_question_only] for mh_attention in mh_attentions]).float().cpu().numpy()
                heatmap_mean[layer_idx][head_idx] = ques_attn.mean()
                ques_attn /= ques_attn.max()
                raw_heatmap[layer_idx][head_idx]= ques_attn.mean()

    logger.info(f"raw : {raw_heatmap[0][0].shape}")
    # logger.info(f"raw : {raw_heatmap}")
    heatmap_mean_df = pd.DataFrame(heatmap_mean)
    logger.info(f"dataframe shape : {heatmap_mean_df.shape}")

    fig = plt.figure(figsize=(num_layers,num_heads)) 
    ax = seaborn.heatmap(heatmap_mean_df,square=True,annot=True, cmap="coolwarm",cbar_kws={"orientation": "vertical","shrink":0.3})
    ax.set_xlabel("Layers")
    ax.set_ylabel("Heads")
    ax.set_title(f"{attn_modality_select} Mean Attention")
    fig.tight_layout()
    fig.savefig(state.attention_key + 'mean_image_to_answer.png')

    if attn_modality_select == "Image-to-Answer":
        fig2,ax2 = plt.subplots(nrows=num_heads, ncols=num_layers, figsize=(num_layers,num_heads))
        for i in range(num_heads):
            for j in range(num_layers):
                ax2[i][j].imshow(raw_heatmap[j][i],cmap="coolwarm")
                ax2[i][j].axis("off")

        # Add labels for x-axis (layers) and y-axis (heads)
        for j in range(num_layers):
            ax2[0][j].set_title(f"Layer {j}", fontsize=10)  # Add titles to the top of each column
        for i in range(num_heads):
            ax2[i][0].set_ylabel(f"Head {i}", fontsize=10, rotation=0, labelpad=30, ha='right')  # Add labels to the first column
        fig2.tight_layout()
        fig2.savefig(state.attention_key + 'image_to_answer_raw.png')
    elif attn_modality_select == "Question-to-Answer":
        raw_normalized_df = pd.DataFrame(raw_heatmap)
        fig2 = plt.figure(figsize=(num_layers,num_heads)) 
        ax = seaborn.heatmap(raw_normalized_df,square=True,annot=True, cmap="coolwarm",cbar_kws={"orientation": "vertical","shrink":0.3})
        ax.set_xlabel("Layers")
        ax.set_ylabel("Heads")
        ax.set_title(f"{attn_modality_select} Max Normalized mean Attention")
        fig.tight_layout()
        fig2.tight_layout()
        fig.savefig(state.attention_key + 'question_to_answer1.png')
        fig2.savefig(state.attention_key + 'question_to_answer2.png')

    return state, fig,fig2

def plot_text_to_image_analysis(state, layer_idx, boxes, head_idx):

    fn_attention = state.attention_key + '_attn.pt'
    img_recover = state.recovered_image
    img_idx = state.image_idx
    generated_text = state.output_ids_decoded

    # Sliders start at 1
    img_patches = [(j, i) for i, row in enumerate(boxes) for j, clicked in enumerate(row) if clicked]
    if len(img_patches) == 0:
        img_patches = [(12,12)]
        logger.info(f"No Patch given used middle point {img_patches}")
    if os.path.exists(fn_attention):
        attentions = torch.load(fn_attention,weights_only=True)
        logger.info(f'Loaded attention from {fn_attention}')
        if len(attentions) == len(state.output_ids_decoded):
            gr.Error('Mismatch between lengths of attentions and output tokens')
        
        # num_tokens = len(attentions)
        num_layers = len(attentions[0])
        # last_mh_attention = attentions[0][-1]
        batch_size, num_heads, inp_seq_len, seq_len = attentions[0][0].shape
        generated_text = state.output_ids_decoded
    
    else:
        return state, None

    layer_mh_attns = defaultdict(dict)
    for j in range(num_layers):
        mh_attentions = []
        for head_id in range(num_heads):
            att_per_head = []
            for i, out_att in enumerate(attentions):
                mh_attention = out_att[j]
                mh_attention = mh_attention[:, :, -1, :].unsqueeze(2)
                att_img = mh_attention.squeeze()[head_id, img_idx:img_idx+576].reshape(24,24)
                att_per_head.append(att_img)
            att_per_head = torch.stack(att_per_head)
            mh_attentions.append(att_per_head)
        mh_attentions = torch.stack(mh_attentions)
        layer_mh_attns[j]=mh_attentions

    mh_attentions = layer_mh_attns[layer_idx]

    logger.info(layer_mh_attns.keys())
    logger.info(layer_mh_attns[0].shape)

    img_mask = np.zeros((24, 24))
    for img_patch in img_patches:
        img_mask[img_patch[0], img_patch[1]] = 1
    img_mask = cmap(img_mask)
    img_mask = Image.fromarray((img_mask[:, :, :3] * 255).astype(np.uint8)).resize((336, 336), Image.BICUBIC)
    img_mask.putalpha(208)
    img_patch_recovered = img_recover.copy()
    img_patch_recovered.paste(img_mask, mask=img_mask)
    img_patch_recovered

    words = generated_text
    float_values = torch.mean(torch.stack([mh_attentions[head_idx, :, x, y] for x, y in img_patches]), dim=0).float().cpu()    
    normalized_values = (float_values - float_values.min()) / (float_values.max() - float_values.min())

    fig = plt.figure(figsize=(15, 8))
    gs = gridspec.GridSpec(1, 2, width_ratios=[1, 3])  # 2 columns, first column for the image, second column for the words
    ax_img = plt.subplot(gs[0])
    ax_img.imshow(img_patch_recovered)
    ax_img.axis('off')
    ax_words = plt.subplot(gs[1])
    x_position = 0.0

    for word, value in zip(words, normalized_values):
        # color = plt.get_cmap("coolwarm")(value)
        color = plt.get_cmap("coolwarm")(value)
        color = to_rgba(color, alpha=0.6) 
        ax_words.text(x_position, 0.5, word, color=color, fontsize=12, ha='left', va='center')
        x_position += 0.05 

    cax = fig.add_axes([0.1, 0.15, 0.8, 0.03])  
    norm = plt.Normalize(min(normalized_values), max(normalized_values))
    # sm = plt.cm.ScalarMappable(cmap="coolwarm", norm=norm)
    sm = plt.cm.ScalarMappable(cmap="coolwarm", norm=norm)
    sm.set_array([]) 
    cb = fig.colorbar(sm, cax=cax, orientation='horizontal')
    cb.set_label('Color Legend', labelpad=2, loc="center")

    ax_words.axis('off')
    plt.suptitle(f"Attention to the selected image patch(es) of head #{head_idx} and layer #{layer_idx}", fontsize=16, y=0.8, x=0.6)    
    plt.savefig(state.attention_key + 'attention_to_the_selected_patches.png')
    
    def stacked_patch(mh):
        return torch.stack([mh[:, :, x, y] for x, y in img_patches]).mean(0).float().cpu().mean(-1)

    # attn_heatmap = plt.figure(figsize=(10, 3))
    # attn_image_patch =  mh_attentions[:, :, img_patch[0], img_patch[1]].cpu().mean(-1)
    attn_image_patch = torch.stack([mh_attentions[:, :, x, y] for x, y in img_patches]).mean(0).float().cpu().mean(-1)
    logger.debug(torch.stack([mh_attentions[:, :, x, y] for x, y in img_patches]).shape)
    logger.debug(torch.stack([mh_attentions[:, :, x, y] for x, y in img_patches]).mean(0).shape)
    logger.debug(attn_image_patch.shape)
    
    fig2,ax2 = plt.subplots(nrows= num_layers,figsize=(num_heads*2, num_layers*2))
    for j in range(num_layers):
        seaborn.heatmap([stacked_patch(layer_mh_attns[j])], 
            linewidths=.3,annot=True, cmap="coolwarm",ax=ax2[j],cbar_kws={"orientation": "vertical", "shrink":0.3}
        )
        ax2[j].set_ylabel(f'Layer "{j}')
    ax2[-1].set_xlabel('Head number')
    ax2[-1].set_title(f"Mean Head Attention between the image patches selected and the answer for all layers")
    fig2.tight_layout()
    fig2.savefig(state.attention_key + 'mean_head_attn_all_layers.png')
    return state, fig, fig2

def attention_rollout(state,fusion_method="min",cls_idx=0,topk=0.0,start_roll=0):
    """
    Experimental Implementation
    """
    fn_attention = state.attention_key + '_attn.pt'
    img_recover = state.recovered_image
    token_idx=0
    img_idx = state.image_idx
    discard_ratio=topk
    # generated_text = state.output_ids_decoded

    if os.path.exists(fn_attention):
        attentions = torch.load(fn_attention,weights_only=True)
        logger.info(f'Loaded attention from {fn_attention}')
        if len(attentions) == len(state.output_ids_decoded):
            gr.Error('Mismatch between lengths of attentions and output tokens')
        
        num_tokens = len(attentions)
        num_layers = len(attentions[token_idx])
    else:
        gr.Error("did not find attention")
        return

    assert start_roll < num_layers , f"{start_roll=} should be less than {num_layers=}"

    attn = attentions[token_idx]
    # _,_,q_size,k_size = attn[0].shape
    # assert q_size == k_size , "we will calculate only for first token"

    I = torch.eye(cls_idx + img_idx+576)
    roll_map = torch.eye(cls_idx+img_idx+576)

    for layer_idx in range(start_roll,num_layers):
        layer_map = einops.rearrange(attn[layer_idx],
                                    "1 h q k -> h q k")
        fused_map = einops.reduce(layer_map,
                                  "h q k -> q k",fusion_method)
        fused_map = fused_map[cls_idx:cls_idx+img_idx+576,
                              cls_idx:cls_idx+img_idx+576].float()

        _, indices = fused_map.topk(int(fused_map.size(-1)*discard_ratio), -1, False)
        #indices = indices[indices != 0]
        fused_map[0, indices] = 0

        roll_map = roll_map @ (fused_map+I)/2
        # this makes a lower triangular matrix (use sum dim as 1)
        roll_map = roll_map/roll_map.sum(dim=-1,keepdim=True)

    # fetch the img_features
    roll_map = roll_map[img_idx:img_idx+576, img_idx:img_idx+576] 
    column_major_map = roll_map[:,0].view(24,-1)
    column_major_map = column_major_map/column_major_map.max()

    diag_map = torch.diag(roll_map).view(24,-1)
    diag_map = diag_map/diag_map.max()
    
    fig= plt.figure(figsize=(15,8))
    # ax[0].imshow(column_major_map,cmap="coolwarm")
    # ax[0].set_ylabel("Debugging Column Major")
    # ax[0].colorbar()
    plt.imshow(diag_map,cmap="coolwarm")
    plt.title("Diagnoally sourced attention rollout")
    plt.colorbar()
    plt.suptitle("Attention Rollout")


    # img_overlay_attn1 = draw_heatmap_on_image(column_major_map, img_recover)
    img_overlay_attn2 = draw_heatmap_on_image(diag_map, img_recover)

    return fig,img_overlay_attn2

def attention_flow(state,fusion_method= "min",cls_idx=0,topk=0.2,start_flow=0):
    """
    Experimental Implementation
    Computes Attention Flow to determine the strongest path between the class token and image tokens.
    """

    fn_attention = state.attention_key + '_attn.pt'
    img_recover = state.recovered_image
    token_idx=0
    img_idx = state.image_idx
    discard_ratio=topk

    if os.path.exists(fn_attention):
        attentions = torch.load(fn_attention,weights_only=True)
        logger.info(f'Loaded attention from {fn_attention}')
        if len(attentions) == len(state.output_ids_decoded):
            gr.Error('Mismatch between lengths of attentions and output tokens')
        
        num_tokens = len(attentions)
        num_layers = len(attentions[token_idx])
    else:
        gr.Error("did not find attention")
        return

    attn = attentions[token_idx]
    num_layers = len(attn)
    img_idx_end = cls_idx + img_idx + 576 
    assert start_flow < num_layers , f"{start_flow=} should be less than {num_layers=}"

    # Initialize flow map as identity matrix
    flow_map = torch.eye(img_idx_end)
    
    # Compute attention flow across layers
    for layer_idx in range(start_flow,num_layers):
        layer_map = einops.rearrange(attn[layer_idx], "1 h q k -> h q k")
        fused_map = einops.reduce(layer_map, "h q k -> q k", fusion_method)
        
        fused_map = fused_map[cls_idx:img_idx_end,
                              cls_idx:img_idx_end]

        # discard less salient patches (similar to what we did in rollout)
        # does not work !!
        # _, indices = fused_map.topk(int(fused_map.size(-1)*discard_ratio), -1, False)
        # #indices = indices[indices != 0]
        # fused_map[0, indices] = 0

        # instead of discarding maybe we can give an option to use any other
        # column than column 0. as they are decresing in nature no? 
        
        # Normalize attention on keys
        fused_map = fused_map / fused_map.sum(dim=-1, keepdim=True)
        
        # 🫣 todo: double check this later
        flow_map = torch.max(
            einops.rearrange(flow_map, "q k -> 1 q k"),
            einops.rearrange(fused_map, "q k -> q 1 k")
        ).max(dim=0).values

    flow_map = flow_map[img_idx:img_idx_end,
                        img_idx:img_idx_end]
    flow_columnar = flow_map[0,:].view(24,-1)

    fig,ax = plt.subplots(1,2,figsize=(15,8))
    ax[0].imshow(flow_map,cmap="viridis")
    ax[0].set_ylabel("Flow strength")

    ax[1].imshow(flow_columnar,cmap="coolwarm")
    ax[1].set_ylabel("Attention Flow")
    fig.savefig(state.attention_key + '_flow.png')

    flow_img = draw_heatmap_on_image(flow_columnar, img_recover)
    return fig,flow_img

def reset_tokens(state):
    generated_text = []
    for text in state.output_ids_decoded:
        generated_text.extend([(text, None), (' ', None)])

    return generated_text



def select_all_tokens(state):
    selected_tokens = []
    for text in state.output_ids_decoded:
        selected_tokens.extend([(text, True), (' ', True)])
    # print(selected_tokens)
    return selected_tokens
