import os
import tempfile
import logging

from gradio.external import re
from matplotlib import interactive
from numpy.ma import maximum, minimum
import torch

from PIL import Image
import numpy as np
import gradio as gr
import spaces

from torchvision.transforms.functional import to_pil_image

from utils_model import get_processor_model, move_to_device, to_gradio_chatbot, process_image

from utils_attn import (
    attention_rollout, handle_attentions_i2t, plot_attention_analysis, handle_relevancy, handle_text_relevancy, reset_tokens,select_all_tokens,
    plot_text_to_image_analysis, handle_box_reset, boxes_click_handler, attn_update_slider,
    attention_rollout, attention_flow
)

from utils_relevancy import construct_relevancy_map

from utils_causal_discovery import (
    handle_causality, handle_causal_head, causality_update_dropdown
)

logger = logging.getLogger(__name__)

N_LAYERS = 32 
CUR_DIR = os.path.dirname(os.path.abspath(__file__))
ROLE0 = "USER"
ROLE1 = "ASSISTANT"

processor = None
model = None

system_prompt = """You are a helpful, respectful and honest assistant. Always answer as helpfully as possible, while being safe.  Your answers should not include any harmful, unethical, racist, sexist, toxic, dangerous, or illegal content. Please ensure that your responses are socially unbiased and positive in nature.
If a question does not make any sense, or is not factually coherent, explain why instead of answering something not correct. If you don't know the answer to a question, please don't share false information."""

# system_prompt = ""
# system_prompt ="""A chat between a curious human and an artificial intelligence assistant. The assistant gives helpful, detailed, and polite answers to the human's questions."""

title_markdown = ("""
# A Saliency Inspection tool for Vision Language Models
""")

tos_markdown = ("""
#### Terms of use
A Fork mainted for : *#TODO!*
By using this service, users are required to agree to the following terms:
##### The service is a research preview intended for non-commercial use only. It only provides limited safety measures and may generate offensive content. It must not be used for any illegal, harmful, violent, racist, or sexual purposes.
""")

authors_markdown = (
        """
        ### Developed and maintained by Chang and me
        ```
        TODO add citations
        ```
        """
)

block_css = """
#image_canvas canvas {
    max-width: 400px !important;
    max-height: 400px !important;
}

#buttons button {
    min-width: min(120px,100%);
}
"""

def clear_history(request: gr.Request):
    logger.info(f"clear_history. ip: {request.client.host}")
    state = gr.State()
    state.messages = []
    return (state, [], "", None, None, None, None)

def add_text(state, text, image, image_process_mode):
    if True: # state is None:
        state = gr.State()
        state.messages = []
        
    if isinstance(image, dict):
        image = image['composite']
        background = Image.new('RGBA', image.size, (255, 255, 255))
        image = Image.alpha_composite(background, image).convert('RGB')

        # ImageEditor does not return None image
        if (np.array(image)==255).all():
            image =None

    text = text[:1536]  # Hard cut-off
    logger.info(text)

    prompt_len = 0
    # prompt=f"[INST] {system_prompt} [/INST]\n\n" if system_prompt else ""
    if processor.tokenizer.chat_template is not None:
        prompt = processor.tokenizer.apply_chat_template(
            [{"role": "user", "content": "<image>\n" + text}],
            tokenize=False,
            add_generation_prompt=True,
        )
        prompt_len += len(prompt)
    else:
        prompt = system_prompt
        prompt_len += len(prompt)
        if image is not None:
            msg = f"\n{ROLE0}: <image>\n{text}\n{ROLE1}:" # Ignore <image> token when calculating prompt length\     
        else:
            msg = f"\n{ROLE0}: {text}\n{ROLE1}: "
        prompt += msg
        prompt_len += len(msg)

    state.messages.append([ROLE0,  (text, image, image_process_mode)])
    state.messages.append([ROLE1, None])

    state.prompt_len = prompt_len
    state.prompt = prompt
    state.image = process_image(image, image_process_mode, return_pil=True)

    return (state, to_gradio_chatbot(state), "", None)


@spaces.GPU
def lvlm_bot(state, temperature, top_p, max_new_tokens):   
    prompt = state.prompt
    prompt_len = state.prompt_len
    image = state.image
    
    inputs = processor(text=prompt,images= image,
                       return_tensors="pt").to(model.device)
    input_ids = inputs.input_ids
    img_idx = torch.where(input_ids==model.config.image_token_index)[1][0].item()
    do_sample = True if temperature > 0.001 else False
    # Generate
    model.enc_attn_weights = []
    model.enc_attn_weights_vit = []

    if model.language_model.config.model_type == "gemma":
        eos_token_id = processor.tokenizer('<end_of_turn>', add_special_tokens=False).input_ids[0]
    else:
        eos_token_id = processor.tokenizer.eos_token_id

    outputs = model.generate(
            **inputs, 
            do_sample=do_sample,
            temperature=temperature,
            top_p=top_p,
            max_new_tokens=max_new_tokens,
            use_cache=True,
            output_attentions=True,
            return_dict_in_generate=True,
            output_scores=True,
            eos_token_id=eos_token_id
        )

    input_ids_list = input_ids.reshape(-1).tolist()
    input_ids_list[img_idx] = 0
    input_text = processor.tokenizer.decode(input_ids_list) # eg. "<s> You are a helpful ..."
    if input_text.startswith("<s> "):
        input_text = '<s>' + input_text[4:] # Remove the first space after <s> to maintain correct length
    input_text_tokenized = processor.tokenizer.tokenize(input_text) # eg. ['<s>', '▁You', '▁are', '▁a', '▁helpful', ... ]
    input_text_tokenized[img_idx] = "average_image"
    
    output_ids = outputs.sequences.reshape(-1)[input_ids.shape[-1]:].tolist()  

    generated_text = processor.tokenizer.decode(output_ids)
    output_ids_decoded = [processor.tokenizer.decode(oid).strip() for oid in output_ids] # eg. ['The', 'man', "'", 's', 'sh', 'irt', 'is', 'yellow', '.', '</s>']
    generated_text_tokenized = processor.tokenizer.tokenize(generated_text)

    logger.info(f"Generated response: {generated_text}")
    logger.debug(f"output_ids_decoded: {output_ids_decoded}")
    logger.debug(f"generated_text_tokenized: {generated_text_tokenized}")

    state.messages[-1][-1] = generated_text[:-len('</s>')] if generated_text.endswith('</s>') else generated_text

    tempdir = os.getenv('TMPDIR', '/tmp/')
    tempfilename = tempfile.NamedTemporaryFile(dir=tempdir)
    tempfilename.close()

    # Save input_ids and attentions
    fn_input_ids = f'{tempfilename.name}_input_ids.pt'
    logger.info(f"Input ids saved to {fn_input_ids}")
    torch.save(move_to_device(input_ids, device='cpu'), fn_input_ids)

    fn_attention = f'{tempfilename.name}_attn.pt'
    torch.save(move_to_device(outputs.attentions, device='cpu'), fn_attention)
    logger.info(f"Attention saved to : {fn_attention}")

    fn_output_ids = f'{tempfilename.name}_output_ids.pt'
    logger.info(f"Saved attention to {fn_attention}")
    torch.save(torch.tensor(output_ids),fn_output_ids)

    model.enc_attn_weights = []
    model.enc_attn_weights_vit = []
    # enc_attn_weights_vit = []
    # rel_maps = []

    # Reconstruct processed image
    img_std = torch.tensor(processor.image_processor.image_std).view(3,1,1)
    img_mean = torch.tensor(processor.image_processor.image_mean).view(3,1,1)
    img_recover = inputs.pixel_values[0].cpu() * img_std + img_mean
    img_recover = to_pil_image(img_recover)

    state.recovered_image = img_recover
    state.input_text_tokenized = input_text_tokenized
    state.output_ids_decoded = output_ids_decoded 
    state.attention_key = tempfilename.name
    state.image_idx = img_idx

    return state, to_gradio_chatbot(state) 


def build_demo(args, embed_mode=False):
    global model
    global processor
    global system_prompt
    global ROLE0
    global ROLE1

    if model is None:
        processor, model = get_processor_model(args)

    if 'gemma' in args.model_name_or_path:
        system_prompt = ''
        ROLE0 = 'user'
        ROLE1 = 'model'

    textbox = gr.Textbox(show_label=False, placeholder="Enter text and press ENTER", container=False)
    with gr.Blocks(title="Sailency Inspector Experimental", theme=gr.themes.Default(), css=block_css) as demo:
        state = gr.State()

        if not embed_mode:
            gr.Markdown(title_markdown)

        with gr.Tab("Generation"):
            with gr.Row():
                with gr.Column(scale=6):
                    imagebox = gr.ImageEditor(type="pil", height=400, elem_id="image_canvas",eraser=True,brush=True)
                    with gr.Accordion("Parameters", open=False) as parameter_row:
                        image_process_mode = gr.Radio(
                            ["Crop", "Resize", "Pad", "Default"],
                            value="Default",
                            label="Preprocess for non-square image", visible=True
                        )
                        temperature = gr.Slider(minimum=0.0, maximum=1.0, value=0.2, step=0.1, interactive=True, label="Temperature",)
                        top_p = gr.Slider(minimum=0.0, maximum=1.0, value=0.7, step=0.1, interactive=True, label="Top P",)
                        max_output_tokens = gr.Slider(minimum=0, maximum=512, value=64, step=64, interactive=True, label="Max new output tokens",)


                with gr.Column(scale=6):
                    chatbot = gr.Chatbot(elem_id="chatbot", label="Chatbot", height=400)
                    with gr.Row():
                        with gr.Column(scale=8):
                            textbox.render()
                        with gr.Column(scale=1, min_width=50):
                            submit_btn = gr.Button(value="Send", variant="primary")
                    with gr.Row(elem_id="buttons") as button_row:
                        clear_btn = gr.Button(value="🗑️  Clear", interactive=True, visible=True)

        with gr.Tab("Mean Token Image-to-Answer"):
            gr.Markdown("""
            ### How To Interpret:
            ```
            * Mean (All output tokens) Influence of image patches to llm response.
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
            ```

            """)
            with gr.Row():
                attn_ana_plot = gr.Plot(label="Attention plot")
            with gr.Row():
                # attn_ana_layer = gr.Slider(1, 100, step=1, label="Layer")
                attn_modality_select = gr.State("Image-to-Answer")
                attn_ana_submit = gr.Button(value="Plot attention matrix", interactive=True)
            with gr.Row():
                raw_mean_plot = gr.Plot(label="Pre Mean (only collapse mean token)")


        attn_ana_submit.click(
                plot_attention_analysis,
                [state, attn_modality_select],
                [state, attn_ana_plot, raw_mean_plot]
            )

        with gr.Tab("Mean Token Question-to-Answer"):
            gr.Markdown("""
            ### How To Interpret Question to Answer:
            ```
            * Mean (All output tokens) Influence of question tokens to llm response.
            Question tokens to response tokens:
            similar to Image to Answer :
                but here we skip image tokens and only collect :
                mh_attns[img_idx+576:img_idx+576+len(question_tokens)]
            ```
            """)
            with gr.Row():
                attn_ana_plot = gr.Plot(label="Attention plot")
            with gr.Row():
                # attn_ana_layer = gr.Slider(1, 100, step=1, label="Layer")
                attn_modality_select = gr.State("Question-to-Answer")
                attn_ana_submit = gr.Button(value="Plot attention matrix", interactive=True)
            with gr.Row():
                raw_mean_plot = gr.Plot(label="Max Normalized Mean")

        attn_ana_submit.click(
                plot_attention_analysis,
                [state, attn_modality_select],
                [state, attn_ana_plot,raw_mean_plot]
            )

        with gr.Tab("Raw Attentions"):
            gr.Markdown("""
            ### How To Use Raw Attentions:
            ```
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

                sort on head with highest response
            ```
            """)
            # for more details refer : handle_attentions_i2t,
            with gr.Row():
                # image box, select tokens ,[reset,plot]
                # thumbnail input image
                imagebox_recover = gr.Image(type="pil", label='Preprocessed image', interactive=False)
                # box to select tokens to incluede attentions from attn[sel_tokens][all_layers]
                generated_text = gr.HighlightedText(
                    label="Generated text (tokenized)",
                    combine_adjacent=False,
                    interactive=True,
                    color_map={"label": "green"}
                )

            # buttons to interact with generated text
            with gr.Row():
                select_all = gr.Button(value="Select All Tokens",interactive=True)
                attn_reset = gr.Button(value="Reset tokens", interactive=True)

            with gr.Row():
                attn_submit = gr.Button(value="Plot attention", interactive=True)
            with gr.Row():
                # heatmap for mean attention across all layers
                # i2t_attn_head_mean_plot = gr.Plot(label="Image-to-Text attention average per head")
                i2t_attn_head_mean_plot = gr.Plot(label="Raw attention per head for all layers")
            with gr.Row():
                # saliency over all heads and all layers
                i2t_attn_gallery = gr.Gallery(type="pil", label='Attention heatmaps', columns=8, interactive=False)

        with gr.Tab("Attention Rollout [Experimental]"):
            with gr.Row():
                gr.Markdown("""```
                            For more details read paper [1]
                            we calculate a summarized version of attention from the vision tower using attention rollout.
                            The saliency is independent of the text input ids.
                            This fuses across all heads (choose fusion method)
                            and across all layers with: `A(li) =A(li) A(li)-1 `
                            ```
                            """)

            with gr.Row():
                fusion_method_rollout = gr.Dropdown(choices=["mean","min","max"],value="min",label="Fusion Method")
                cls_index = gr.Slider(minimum=0,maximum=20,step=1,label="Debugging cls index")
                start_rolling = gr.Slider(minimum=0,maximum=N_LAYERS-1,step=1,label="Start Rolling From")
                topk_rollout = gr.Slider(minimum=0,maximum=1,step=0.1,label="1 - top k saliency (discard ratio)")
                rollout_submit = gr.Button(value="Plot rollout",interactive=True)

            with gr.Row():
                rollout_plot = gr.Plot(label="rollout plot")
                rollout_overlay2 = gr.Image(label="Rollout overlay diagnoal",interactive=False)
            # with gr.Row():
            #     rollout_overlay1 = gr.Image(label="Rollout overlay column major",interactive=False)
            #     rollout_overlay2 = gr.Image(label="Rollout overlay diagnoal",interactive=False)
            with gr.Row():
                gr.Markdown("""
                            ```
                            @misc{abnar2020quantifyingattentionflowtransformers,
                            title={Quantifying Attention Flow in Transformers}, 
                            author={Samira Abnar and Willem Zuidema},
                            year={2020},
                            eprint={2005.00928},
                            archivePrefix={arXiv},
                            primaryClass={cs.LG},
                            url={https://arxiv.org/abs/2005.00928}, 
                            }[1]
                            ```
                            """)

            rollout_submit.click(
                attention_rollout,
                [state, fusion_method_rollout,cls_index,topk_rollout,start_rolling],
                [rollout_plot,rollout_overlay2]
            )

        with gr.Tab("Attention Flow [Experimental]"):
            with gr.Row():
                gr.Markdown("""
                            ```
                            Treating the attention graph as a flow network,
                            where the capacities of the edges are attention
                            weights, using any maximum flow algorithm, we
                            can compute the maximum attention flow from any
                            node in any of the layers to any of the input nodes.[1]
                            ```
                            """)
            with gr.Row():
                fusion_method_flow = gr.Dropdown(choices=["mean","min","max"],value="min",label="Fusion Method")
                cls_idx_rollout = gr.Slider(minimum=0,maximum=20,step=1,label="Debugging cls index")
                start_flowing = gr.Slider(minimum=0,maximum=N_LAYERS-1,step=1,label="Start flow From")
                topk_rollout = gr.Slider(minimum=0,maximum=1,step=0.05,label="Discard Ratio")
                flow_submit = gr.Button(value="Plot Attention",interactive=True)
            with gr.Row():
                flow_strength_plot = gr.Plot(label="Flow strength plot")
                flow_overlay = gr.Image(label="Columnar Sourced")
            with gr.Row():
                gr.Markdown("""
                            ```
                            @misc{abnar2020quantifyingattentionflowtransformers,
                            title={Quantifying Attention Flow in Transformers}, 
                            author={Samira Abnar and Willem Zuidema},
                            year={2020},
                            eprint={2005.00928},
                            archivePrefix={arXiv},
                            primaryClass={cs.LG},
                            url={https://arxiv.org/abs/2005.00928}, 
                            }[1]
                            ```
                            """)
                
            flow_submit.click(
                attention_flow,
                [state, fusion_method_flow,cls_idx_rollout,topk_rollout,start_flowing],
                [flow_strength_plot,flow_overlay]
            )


        with gr.Tab("Patch to response"):
            with gr.Row():
                with gr.Column():
                    box_states = gr.Dataframe(type="numpy", datatype="bool", row_count=24, col_count=24, visible=False) 
                    imagebox_recover_boxable = gr.Image(label='Patch Selector')
                with gr.Column():
                    attn_select_layer = gr.Slider(0, N_LAYERS, step=1, label="Layer")
                    attn_ana_head= gr.Slider(0, 16, step=1, label="Head Index")
                    reset_boxes_btn = gr.Button(value="Reset patch selector")
                    attn_ana_submit_2 = gr.Button(value="Plot attention matrix", interactive=True)
                
            with gr.Row():
                t2i_attn_head_mean_plot = gr.Plot(label="Text-to-Image attention average per head")
            with gr.Row():
                attn_ana_plot_2 = gr.Plot(label="Attention plot")

        
        reset_boxes_btn.click(
            handle_box_reset, 
            [imagebox_recover,box_states], 
            [imagebox_recover_boxable, box_states]
        )
        imagebox_recover_boxable.select(boxes_click_handler, [imagebox_recover,box_states], [imagebox_recover_boxable, box_states])


        select_all.click(
            select_all_tokens,
            [state],
            [generated_text]
        )

                
        attn_reset.click(
            reset_tokens,
            [state],
            [generated_text]
        )

        attn_ana_submit_2.click(
            plot_text_to_image_analysis,
            [state, attn_select_layer, box_states, attn_ana_head ],
            [state, attn_ana_plot_2, t2i_attn_head_mean_plot]
        )
        

        attn_submit.click(
            handle_attentions_i2t,
            [state, generated_text],
            [generated_text, imagebox_recover, i2t_attn_gallery, i2t_attn_head_mean_plot]
        )



        if not embed_mode:
            gr.Markdown(tos_markdown)
            gr.Markdown(authors_markdown)

        clear_btn.click(
            clear_history,
            None,
            [state, chatbot, textbox, imagebox, imagebox_recover, generated_text, i2t_attn_gallery ] ,
            queue=False
        )

        textbox.submit(
            add_text,
            [state, textbox, imagebox, image_process_mode],
            [state, chatbot, textbox, imagebox],
            queue=False
        ).then(
            lvlm_bot,
            [state, temperature, top_p, max_output_tokens],
            [state, chatbot] ,
        ).then(
            attn_update_slider,
            [state],
            [state, attn_select_layer]
        )

        submit_btn.click(
            add_text,
            [state, textbox, imagebox, image_process_mode],
            [state, chatbot, textbox, imagebox],
            queue=False
        ).then(
            lvlm_bot,
            [state, temperature, top_p, max_output_tokens],
            [state, chatbot],
        ).then(
            attn_update_slider,
            [state],
            [state, attn_select_layer]
        )


    return demo


