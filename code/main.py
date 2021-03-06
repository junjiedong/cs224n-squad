# Copyright 2018 Stanford University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This file contains the entrypoint to the rest of the code"""



import os
import io
import json
import sys
import logging

import tensorflow as tf

from qa_model import QAModel
from vocab import get_glove
from official_eval_helper import get_json_data, generate_answers
from bilm import Batcher
# NOTE: CHANGE (ENSEMBLE)
from official_eval_helper import generate_partial_answers, generate_ensemble_answers


logging.basicConfig(level=logging.INFO)

MAIN_DIR = os.path.relpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) # relative path of the main directory
DEFAULT_DATA_DIR = os.path.join(MAIN_DIR, "data") # relative path of data dir
EXPERIMENTS_DIR = os.path.join(MAIN_DIR, "experiments") # relative path of experiments dir


# High-level options
tf.app.flags.DEFINE_integer("gpu", 0, "Which GPU to use, if you have multiple.")
tf.app.flags.DEFINE_string("mode", "train", "Available modes: train / show_examples / official_eval")
tf.app.flags.DEFINE_string("experiment_name", "", "Unique name for your experiment. This will create a directory by this name in the experiments/ directory, which will hold all data related to this experiment")
tf.app.flags.DEFINE_integer("num_epochs", 0, "Number of epochs to train. 0 means train indefinitely")

# Hyperparameters
tf.app.flags.DEFINE_float("learning_rate", 0.001, "Learning rate.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0, "Clip gradients to this norm.")
tf.app.flags.DEFINE_float("dropout", 0.2, "Fraction of units randomly dropped on non-recurrent connections.")
tf.app.flags.DEFINE_integer("batch_size", 100, "Batch size to use")
tf.app.flags.DEFINE_integer("hidden_size", 100, "Size of the hidden states")
tf.app.flags.DEFINE_integer("context_len", 400, "The maximum context length of your model")
tf.app.flags.DEFINE_integer("question_len", 30, "The maximum question length of your model")
tf.app.flags.DEFINE_integer("embedding_size", 300, "Size of the pretrained word vectors. This needs to be one of the available GloVe dimensions: 50/100/200/300")
tf.app.flags.DEFINE_boolean("share_LSTM_weights", True, "Whether to share encoder LSTM weights for context and question")
tf.app.flags.DEFINE_integer("max_word_size", 40, "Size of max lenght of a token (word)")
tf.app.flags.DEFINE_integer("elmo_embedding_max_token_size", 60, "Size of max lenght of a token (word)")
tf.app.flags.DEFINE_integer("pos_embedding_size", 10, "Size of pos embedding")
tf.app.flags.DEFINE_integer("ne_embedding_size", 10, "Size of name entity embedding")
tf.app.flags.DEFINE_integer("char_embedding_size", 16, "Size of char embedding")
tf.app.flags.DEFINE_integer("num_of_char", 262, "Size of char embedding")
# NOTE: Change
tf.app.flags.DEFINE_boolean("load_ema_checkpoint", True, "Which checkpoint to load (ema/original)")
# NOTE: CHANGE (ENSEMBLE)
tf.app.flags.DEFINE_string("single_ensemble", "", "Whether to use the single model or ensemble model")

# How often to print, save, eval
tf.app.flags.DEFINE_integer("print_every", 1, "How many iterations to do per print.")
tf.app.flags.DEFINE_integer("save_every", 500, "How many iterations to do per save.")
tf.app.flags.DEFINE_integer("eval_every", 500, "How many iterations to do per calculating loss/f1/em on dev set. Warning: this is fairly time-consuming so don't do it too often.")
tf.app.flags.DEFINE_integer("keep", 1, "How many checkpoints to keep. 0 indicates keep all (you shouldn't need to do keep all though - it's very storage intensive).")

# Reading and saving data
tf.app.flags.DEFINE_string("train_dir", "", "Training directory to save the model parameters and other info. Defaults to experiments/{experiment_name}")
tf.app.flags.DEFINE_string("glove_path", "", "Path to glove .txt file. Defaults to data/glove.6B.{embedding_size}d.txt")
tf.app.flags.DEFINE_string("data_dir", DEFAULT_DATA_DIR, "Where to find preprocessed SQuAD data for training. Defaults to data/")
tf.app.flags.DEFINE_string("ckpt_load_dir", "", "For official_eval mode, which directory to load the checkpoint fron. You need to specify this for official_eval mode.")
tf.app.flags.DEFINE_string("json_in_path", "", "For official_eval mode, path to JSON input file. You need to specify this for official_eval_mode.")
tf.app.flags.DEFINE_string("json_out_path", "predictions.json", "Output path for official_eval mode. Defaults to predictions.json")
tf.app.flags.DEFINE_string("main_dir", MAIN_DIR, "The main directory.")


FLAGS = tf.app.flags.FLAGS
os.environ["CUDA_VISIBLE_DEVICES"] = str(FLAGS.gpu)


def initialize_model(session, model, train_dir, expect_exists):
    """
    Initializes model from train_dir.

    Inputs:
      session: TensorFlow session
      model: QAModel
      train_dir: path to directory where we'll look for checkpoint
      expect_exists: If True, throw an error if no checkpoint is found.
        If False, initialize fresh model if no checkpoint is found.
    """
    print ("Looking for model at %s..." % train_dir)
    ckpt = tf.train.get_checkpoint_state(train_dir)
    v2_path = ckpt.model_checkpoint_path + ".index" if ckpt else ""
    if ckpt and (tf.gfile.Exists(ckpt.model_checkpoint_path) or tf.gfile.Exists(v2_path)):
        print ("Reading model parameters from %s" % ckpt.model_checkpoint_path)

        # NOTE: CHANGE
        if FLAGS.load_ema_checkpoint:   # Restore using the EMA weights
            ema_restorer = tf.train.Saver(model.ema.variables_to_restore())
            ema_restorer.restore(session, ckpt.model_checkpoint_path)
        else:   # Restore using the original weights
            model.saver.restore(session, ckpt.model_checkpoint_path)

    else:
        if expect_exists:
            raise Exception("There is no saved checkpoint at %s" % train_dir)
        else:
            print ("There is no saved checkpoint at %s. Creating model with fresh parameters." % train_dir)
            session.run(tf.global_variables_initializer())
            print ('Num params: %d' % sum(v.get_shape().num_elements() for v in tf.trainable_variables()))


def main(unused_argv):
    # Print an error message if you've entered flags incorrectly
    if len(unused_argv) != 1:
        raise Exception("There is a problem with how you entered flags: %s" % unused_argv)

    # Check for Python 2
    if sys.version_info[0] != 3:
        raise Exception("ERROR: You must use Python 3 but you are running Python %i" % sys.version_info[0])

    # Print out Tensorflow version
    print ("This code was developed and tested on TensorFlow 1.4.1. Your TensorFlow version: %s" % tf.__version__)

    # Define train_dir
    if not FLAGS.experiment_name and not FLAGS.train_dir and FLAGS.mode != "official_eval":
        raise Exception("You need to specify either --experiment_name or --train_dir")
    FLAGS.train_dir = FLAGS.train_dir or os.path.join(EXPERIMENTS_DIR, FLAGS.experiment_name)

    # Initialize bestmodel directory
    bestmodel_dir = os.path.join(FLAGS.train_dir, "best_checkpoint")
    # NOTE: CHANGE
    ema_bestmodel_dir = os.path.join(FLAGS.train_dir, "ema_best_checkpoint")

    # Define path for glove vecs
    # NOTE: CHANGE
    FLAGS.glove_path = FLAGS.glove_path or os.path.join(DEFAULT_DATA_DIR, "glove.42B.{}d.txt".format(FLAGS.embedding_size))

    # Load embedding matrix and vocab mappings
    emb_matrix, word2id, id2word = get_glove(FLAGS.glove_path, FLAGS.embedding_size)

    # Get filepaths to train/dev datafiles for tokenized queries, contexts and answers
    train_context_path = os.path.join(FLAGS.data_dir, "train.context")
    train_qn_path = os.path.join(FLAGS.data_dir, "train.question")
    train_ans_path = os.path.join(FLAGS.data_dir, "train.span")
    dev_context_path = os.path.join(FLAGS.data_dir, "dev.context")
    dev_qn_path = os.path.join(FLAGS.data_dir, "dev.question")
    dev_ans_path = os.path.join(FLAGS.data_dir, "dev.span")

    # Initialize model
    qa_model = QAModel(FLAGS, id2word, word2id, emb_matrix)

    # Some GPU settings
    config=tf.ConfigProto()
    config.gpu_options.allow_growth = True

    # Split by mode
    if FLAGS.mode == "train":

        # Setup train dir and logfile
        if not os.path.exists(FLAGS.train_dir):
            os.makedirs(FLAGS.train_dir)
        file_handler = logging.FileHandler(os.path.join(FLAGS.train_dir, "log.txt"))
        logging.getLogger().addHandler(file_handler)

        # Save a record of flags as a .json file in train_dir
        with open(os.path.join(FLAGS.train_dir, "flags.json"), 'w') as fout:
            json.dump(FLAGS.__flags, fout)

        # Make bestmodel dir if necessary
        if not os.path.exists(bestmodel_dir):
            os.makedirs(bestmodel_dir)
        # NOTE: CHANGE
        if not os.path.exists(ema_bestmodel_dir):
            os.makedirs(ema_bestmodel_dir)

        with tf.Session(config=config) as sess:

            # Load most recent model
            initialize_model(sess, qa_model, FLAGS.train_dir, expect_exists=False)

            # Train
            qa_model.train(sess, train_context_path, train_qn_path, train_ans_path, dev_qn_path, dev_context_path, dev_ans_path)


    elif FLAGS.mode == "show_examples":
        with tf.Session(config=config) as sess:

            # Load best model
            # NOTE: CHANGE
            # initialize_model(sess, qa_model, bestmodel_dir, expect_exists=True)
            if FLAGS.load_ema_checkpoint:
                initialize_model(sess, qa_model, ema_bestmodel_dir, expect_exists=True)
            else:
                initialize_model(sess, qa_model, bestmodel_dir, expect_exists=True)

            # Show examples with F1/EM scores
            _, _ = qa_model.check_f1_em(sess, dev_context_path, dev_qn_path, dev_ans_path, "dev", num_samples=10, print_to_screen=True)


    elif FLAGS.mode == "official_eval":
        if FLAGS.json_in_path == "":
            raise Exception("For official_eval mode, you need to specify --json_in_path")
        if FLAGS.ckpt_load_dir == "":
            raise Exception("For official_eval mode, you need to specify --ckpt_load_dir")

        # NOTE: Change
        pos_tag_id_map = {}
        with open(os.path.join(FLAGS.main_dir, 'pos_tags.txt')) as f:
            pos_tag_lines = f.readlines()
        for i in range(len(pos_tag_lines)):
            pos_tag_id_map[pos_tag_lines[i][:-1]] = i + 1 # need to get rid of the trailing newline character
        # get the NE tag to id
        ne_tag_id_map = {}
        all_NE_tag = ['B-FACILITY', 'B-GPE', 'B-GSP', 'B-LOCATION', 'B-ORGANIZATION', 'B-PERSON', 'I-FACILITY', 'I-GPE', 'I-GSP', 'I-LOCATION', 'I-ORGANIZATION', 'I-PERSON','O'] # I know this not elegant
        for i in range(len(all_NE_tag)):
            ne_tag_id_map[all_NE_tag[i]] = i + 1

        # NOTE: CHANGE (ENSEMBLE)
        if FLAGS.single_ensemble == "ensemble":
            # Checkpoint directories of the base models
            num_base_models = 7
            checkpoint_dirs = [os.path.join(FLAGS.ckpt_load_dir, 'ema_best_checkpoint_' + str(i)) for i in range(1, num_base_models+1)]

            partial_answers = []
            for base_model_dir in checkpoint_dirs:
                # Read the JSON data from file
                qn_uuid_data, context_token_data, qn_token_data = get_json_data(FLAGS.json_in_path)
                batcher = Batcher(os.path.join(FLAGS.data_dir, 'elmo_voca.txt'), FLAGS.max_word_size)

                with tf.Session(config=config) as sess:
                    initialize_model(sess, qa_model, base_model_dir, expect_exists=True)
                    partial_answers.append(generate_partial_answers(sess, qa_model, word2id, qn_uuid_data, context_token_data, qn_token_data, batcher, pos_tag_id_map, ne_tag_id_map))

            # # For debugging purpose
            # for i, ans in enumerate(partial_answers):
            #     with open("test"+ str(i+1) + ".txt", 'w') as F:
            #         F.write(str(ans))

            # Aggregate all the partial answers to generate the final answer
            qn_uuid_data, context_token_data, qn_token_data = get_json_data(FLAGS.json_in_path)
            batcher = Batcher(os.path.join(FLAGS.data_dir, 'elmo_voca.txt'), FLAGS.max_word_size)
            ensemble_answer_dict = generate_ensemble_answers(qa_model, word2id, qn_uuid_data, context_token_data, qn_token_data, batcher, pos_tag_id_map, ne_tag_id_map, partial_answers)

            # Write the uuid->answer mapping a to json file in root dir
            print ("Writing predictions to %s..." % FLAGS.json_out_path)
            with io.open(FLAGS.json_out_path, 'w', encoding='utf-8') as f:
                f.write(str(json.dumps(ensemble_answer_dict, ensure_ascii=False)))
                print ("Wrote predictions to %s" % FLAGS.json_out_path)


        else:   # Original official_eval code
            assert FLAGS.single_ensemble == "single"

            batcher = Batcher(os.path.join(FLAGS.data_dir, 'elmo_voca.txt'), FLAGS.max_word_size)

            # Read the JSON data from file
            qn_uuid_data, context_token_data, qn_token_data = get_json_data(FLAGS.json_in_path)

            with tf.Session(config=config) as sess:

                # Load the first model in the checkpoint directory
                checkpoint_dir = os.path.join(FLAGS.ckpt_load_dir, 'ema_best_checkpoint_1')
                initialize_model(sess, qa_model, checkpoint_dir, expect_exists=True)

                # Get a predicted answer for each example in the data
                # Return a mapping answers_dict from uuid to answer
                answers_dict = generate_answers(sess, qa_model, word2id, qn_uuid_data, context_token_data, qn_token_data, batcher, pos_tag_id_map, ne_tag_id_map)

                # Write the uuid->answer mapping a to json file in root dir
                print ("Writing predictions to %s..." % FLAGS.json_out_path)
                with io.open(FLAGS.json_out_path, 'w', encoding='utf-8') as f:
                    f.write(str(json.dumps(answers_dict, ensure_ascii=False)))
                    print ("Wrote predictions to %s" % FLAGS.json_out_path)


    else:
        raise Exception("Unexpected value of FLAGS.mode: %s" % FLAGS.mode)

if __name__ == "__main__":
    tf.app.run()
