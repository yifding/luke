import logging
import functools
import multiprocessing
import queue
import random
import numpy as np

from luke.pretraining.dataset import WikipediaPretrainingDataset
from luke.utils.entity_vocab import MASK_TOKEN

logger = logging.getLogger(__name__)


class LukePretrainingBatchGenerator(object):
    def __init__(self, dataset_dir, mode, batch_size, masked_lm_prob, masked_entity_prob, whole_word_masking,
                 **dataset_kwargs):
        if mode == 'default':
            worker_cls = LukePretrainingBatchWorker
        elif mode == 'e2e':
            worker_cls = LukeE2EPretrainingBatchWorker
        else:
            raise RuntimeError(f'Unsupported mode: {mode}')

        self._worker_func = functools.partial(worker_cls, dataset_dir=dataset_dir, batch_size=batch_size,
                                              masked_lm_prob=masked_lm_prob, masked_entity_prob=masked_entity_prob,
                                              whole_word_masking=whole_word_masking, **dataset_kwargs)

    def generate_batches(self, queue_size=10000):
        output_queue = multiprocessing.Queue(queue_size)
        worker = self._worker_func(output_queue)
        worker.daemon = True
        worker.start()

        try:
            while True:
                try:
                    yield output_queue.get(True, 1)
                except queue.Empty:
                    logger.debug('Queue is empty')
                    if not worker.is_alive():
                        raise RuntimeError('Worker exited unexpectedly')
        finally:
            worker.terminate()
            output_queue.close()


class BaseBatchWorker(multiprocessing.Process):
    def __init__(self, output_queue, dataset_dir, batch_size, masked_lm_prob, whole_word_masking, **dataset_kwargs):
        super(BaseBatchWorker, self).__init__()

        self._output_queue = output_queue
        self._dataset_dir = dataset_dir
        self._batch_size = batch_size
        self._masked_lm_prob = masked_lm_prob
        self._whole_word_masking = whole_word_masking
        self._dataset_kwargs = dataset_kwargs

        if 'shuffle_buffer_size' not in self._dataset_kwargs:
            self._dataset_kwargs['shuffle_buffer_size'] = batch_size * 1000

    def run(self):
        self._pretraining_dataset = WikipediaPretrainingDataset(self._dataset_dir)
        self._tokenizer = self._pretraining_dataset.tokenizer
        self._max_seq_length = self._pretraining_dataset.max_seq_length
        self._max_entity_length = self._pretraining_dataset.max_entity_length
        self._max_mention_length = self._pretraining_dataset.max_mention_length
        self._max_candidate_length = self._pretraining_dataset.max_candidate_length
        self._cls_id = self._tokenizer.vocab[self._tokenizer.cls_token]
        self._sep_id = self._tokenizer.vocab[self._tokenizer.sep_token]
        self._mask_id = self._tokenizer.vocab[self._tokenizer.mask_token]
        self._entity_mask_id = self._pretraining_dataset.entity_vocab[MASK_TOKEN]

        buf = []
        max_word_len = 1
        max_entity_len = 1
        for item in self._pretraining_dataset.create_iterator(**self._dataset_kwargs):
            word_feat = self._create_word_features(item['word_ids'])
            entity_feat = self._create_entity_features(item['entity_ids'], item['entity_position_ids'],
                                                       item['entity_candidate_ids'], item['entity_candidate_labels'])
            max_word_len = max(max_word_len, item['word_ids'].size + 2)  # 2 for [CLS] and [SEP]
            max_entity_len = max(max_entity_len, item['entity_ids'].size)
            buf.append((word_feat, entity_feat))

            if len(buf) == self._batch_size:
                batch = {}
                batch.update({k: np.stack([o[0][k][:max_word_len] for o in buf]) for k in buf[0][0].keys()})
                batch.update({k: np.stack([o[1][k][:max_entity_len] for o in buf]) for k in buf[0][1].keys()})
                self._output_queue.put(batch, True)

                buf = []
                max_word_len = 1
                max_entity_len = 1

    def _create_word_features(self, word_ids):
        output_word_ids = np.zeros(self._max_seq_length, dtype=np.int)
        output_word_ids[:word_ids.size + 2] = np.concatenate([[self._cls_id], word_ids, [self._sep_id]])
        word_attention_mask = np.zeros(self._max_seq_length, dtype=np.int)
        word_attention_mask[:word_ids.size + 2] = 1

        ret = dict(word_ids=output_word_ids,
                   word_attention_mask=word_attention_mask,
                   word_segment_ids=np.zeros(self._max_seq_length, dtype=np.int))

        if self._masked_lm_prob != 0.0:
            num_to_predict = max(1, int(round(word_ids.size * self._masked_lm_prob)))
            candidate_word_indices = []

            for i, word in enumerate(self._tokenizer.convert_ids_to_tokens(word_ids), 1):  # 1 for [CLS]
                if self._whole_word_masking and word.startswith('##') and candidate_word_indices:
                    candidate_word_indices[-1].append(i)
                else:
                    candidate_word_indices.append([i])

            masked_lm_labels = np.full(self._max_seq_length, -1, dtype=np.int)

            for i in np.random.permutation(len(candidate_word_indices)):
                indices_to_mask = candidate_word_indices[i]
                if len(indices_to_mask) > num_to_predict:
                    continue

                for index in indices_to_mask:
                    masked_lm_labels[index] = output_word_ids[index]
                    p = random.random()
                    if p < 0.8:
                        output_word_ids[index] = self._mask_id
                    elif p < 0.9:
                        output_word_ids[index] = random.randint(0, self._tokenizer.vocab_size - 1)
                    num_to_predict -= 1

                if num_to_predict == 0:
                    break

            ret['masked_lm_labels'] = masked_lm_labels

        return ret

    def _create_entity_features(self, entity_ids, entity_position_ids, entity_candidate_ids, entity_candidate_labels):
        raise NotImplementedError()


class LukePretrainingBatchWorker(BaseBatchWorker):
    def __init__(self, output_queue, dataset_dir, batch_size, masked_lm_prob, masked_entity_prob, whole_word_masking,
                 **dataset_kwargs):
        super(LukePretrainingBatchWorker, self).__init__(output_queue, dataset_dir, batch_size, masked_lm_prob,
                                                         whole_word_masking, **dataset_kwargs)

        self._masked_entity_prob = masked_entity_prob

    def _create_entity_features(self, entity_ids, entity_position_ids, entity_candidate_ids, entity_candidate_labels):
        output_entity_ids = np.zeros(self._max_entity_length, dtype=np.int)
        output_entity_ids[:entity_ids.size] = entity_ids

        entity_attention_mask = np.zeros(self._max_entity_length, dtype=np.int)
        entity_attention_mask[:entity_ids.size] = 1

        entity_position_ids += (entity_position_ids != -1)  # +1 for [CLS]
        output_entity_position_ids = np.full((self._max_entity_length, self._max_mention_length), -1, dtype=np.int)
        output_entity_position_ids[:entity_position_ids.shape[0]] = entity_position_ids

        ret = dict(entity_ids=output_entity_ids,
                   entity_position_ids=output_entity_position_ids,
                   entity_attention_mask=entity_attention_mask,
                   entity_segment_ids=np.zeros(self._max_entity_length, dtype=np.int))

        if self._masked_entity_prob != 0.0:
            num_to_predict = max(1, int(round(entity_ids.size * self._masked_entity_prob)))
            masked_entity_labels = np.full(self._max_entity_length, -1, dtype=np.int)
            for index in np.random.permutation(range(entity_ids.size))[:num_to_predict]:
                masked_entity_labels[index] = entity_ids[index]
                output_entity_ids[index] = self._entity_mask_id
            ret['masked_entity_labels'] = masked_entity_labels

        return ret


class LukeE2EPretrainingBatchWorker(LukePretrainingBatchWorker):
    def _create_entity_features(self, entity_ids, entity_position_ids, entity_candidate_ids, entity_candidate_labels):
        output_entity_candidate_ids = np.zeros((self._max_entity_length, self._max_candidate_length), dtype=np.int)
        output_entity_candidate_ids[:entity_candidate_ids.shape[0]] = entity_candidate_ids

        entity_position_ids += (entity_position_ids != -1)  # for [CLS]
        output_entity_position_ids = np.full((self._max_entity_length, self._max_mention_length), -1, dtype=np.int)
        output_entity_position_ids[:entity_position_ids.shape[0]] = entity_position_ids

        entity_attention_mask = np.zeros(self._max_entity_length, dtype=np.int)
        entity_attention_mask[:entity_ids.size] = 1

        output_entity_candidate_labels = np.full(self._max_entity_length, -1, dtype=np.int)
        output_entity_candidate_labels[:entity_candidate_labels.size] = entity_candidate_labels

        ret = dict(entity_candidate_ids=output_entity_candidate_ids,
                   entity_position_ids=output_entity_position_ids,
                   entity_attention_mask=entity_attention_mask,
                   entity_segment_ids=np.zeros(self._max_entity_length, dtype=np.int),
                   entity_candidate_labels=output_entity_candidate_labels)

        if self._masked_entity_prob != 0.0:
            num_to_predict = max(1, int(round(entity_ids.size * self._masked_entity_prob)))
            masked_entity_labels = np.full(self._max_entity_length, -1, dtype=np.int)
            for index in np.random.permutation(range(entity_ids.size))[:num_to_predict]:
                masked_entity_labels[index] = entity_ids[index]
            ret['masked_entity_labels'] = masked_entity_labels

        return ret
