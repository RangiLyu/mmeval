# Copyright (c) OpenMMLab. All rights reserved.
import contextlib
import io
import logging
import numpy as np
import os.path as osp
import tempfile
from collections import OrderedDict
from logging import Logger
from typing import Dict, List, Optional, Sequence, Union

from mmeval.fileio import get_local_path
from .coco_detection import COCODetection

try:
    from lvis import LVIS, LVISEval, LVISResults
    HAS_LVISAPI = True
except ImportError:
    HAS_LVISAPI = False


class LVISDetection(COCODetection):
    """LVIS evaluation metric.

    Evaluate AR, AP for detection tasks on LVIS dataset including proposal/box
    detection and instance segmentation.

    Args:
        ann_file (str): Path to the LVIS dataset annotation file.
        metric (str | List[str]): Metrics to be evaluated. Valid metrics
            include 'bbox', 'segm', and 'proposal'. Defaults to 'bbox'.
        iou_thrs (float | List[float], optional): IoU threshold to compute AP
            and AR. If not specified, IoUs from 0.5 to 0.95 will be used.
            Defaults to None.
        classwise (bool): Whether to return the computed results of each
            class. Defaults to False.
        proposal_nums (int): Numbers of proposals to be evaluated.
            Defaults to 300.
        metric_items (List[str], optional): Metric result names to be
            recorded in the evaluation result. If None, default configurations
            in LVIS will be used.Defaults to None.
        format_only (bool): Format the output results without performing
            evaluation. It is useful when you want to format the result
            to a specific format and submit it to the test server.
            Defaults to False.
        outfile_prefix (str, optional): The prefix of json files. It includes
            the file path and the prefix of filename, e.g., "a/b/prefix".
            If not specified, a temp file will be created. Defaults to None.
        backend_args (dict, optional): Arguments to instantiate the
            preifx of uri corresponding backend. Defaults to None.
        **kwargs: Keyword parameters passed to :class:`BaseMetric`.

    Examples:
        >>> import numpy as np
        >>> from mmeval import LVISDetection
        >>> try:
        >>>     from mmeval.metrics.utils.coco_wrapper import mask_util
        >>> except ImportError as e:
        >>>     mask_util = None
        >>>
        >>> num_classes = 4
        >>> fake_dataset_metas = {
        ...     'classes': tuple([str(i) for i in range(num_classes)])
        ... }
        >>>
        >>> lvis_det_metric = LVISDetection(
        ...     ann_file='data/lvis_v1/annotations/lvis_v1_train.json'
        ...     dataset_meta=fake_dataset_metas,
        ...     metric=['bbox', 'segm']
        ... )
        >>> lvis_det_metric(predictions=predictions)  # doctest: +ELLIPSIS  # noqa: E501
        {'bbox_AP': ..., 'bbox_AP50': ..., ...,
         'segm_AP': ..., 'segm_AP50': ..., ...,}
    """

    def __init__(self,
                 ann_file: str,
                 metric: Union[str, List[str]] = 'bbox',
                 classwise: bool = False,
                 proposal_nums: int = 300,
                 iou_thrs: Optional[Union[float, Sequence[float]]] = None,
                 metric_items: Optional[Sequence[str]] = None,
                 format_only: bool = False,
                 outfile_prefix: Optional[str] = None,
                 backend_args: Optional[dict] = None,
                 logger: Optional[Logger] = None,
                 **kwargs) -> None:
        if not HAS_LVISAPI:
            raise RuntimeError(
                'Package lvis is not installed. Please run "pip install '
                'git+https://github.com/lvis-dataset/lvis-api.git".')
        super().__init__(
            metric=metric,
            classwise=classwise,
            iou_thrs=iou_thrs,
            metric_items=metric_items,
            format_only=format_only,
            outfile_prefix=outfile_prefix,
            backend_args=backend_args,
            **kwargs)
        self.proposal_nums = proposal_nums  # type: ignore

        with get_local_path(
                filepath=ann_file, backend_args=backend_args) as local_path:
            self._lvis_api = LVIS(local_path)

        self.logger = logging.getLogger(__name__) if logger is None else logger

    def add_predictions(self, predictions: Sequence[Dict]) -> None:
        """Add predictions to `self._results`.

        Args:
            predictions (Sequence[dict]): A sequence of dict. Each dict
                representing a detection result for an image, with the
                following keys:

                - img_id (int): Image id.
                - bboxes (numpy.ndarray): Shape (N, 4), the predicted
                  bounding bboxes of this image, in 'xyxy' foramrt.
                - scores (numpy.ndarray): Shape (N, ), the predicted scores
                  of bounding boxes.
                - labels (numpy.ndarray): Shape (N, ), the predicted labels
                  of bounding boxes.
                - masks (list[RLE], optional): The predicted masks.
                - mask_scores (np.array, optional): Shape (N, ), the predicted
                  scores of masks.
        """
        self.add(predictions)

    def add(self, predictions: Sequence[Dict]) -> None:  # type: ignore # yapf: disable # noqa: E501
        """Add the intermediate results to `self._results`.

        Args:
            predictions (Sequence[dict]): A sequence of dict. Each dict
                representing a detection result for an image, with the
                following keys:

                - img_id (int): Image id.
                - bboxes (numpy.ndarray): Shape (N, 4), the predicted
                  bounding bboxes of this image, in 'xyxy' foramrt.
                - scores (numpy.ndarray): Shape (N, ), the predicted scores
                  of bounding boxes.
                - labels (numpy.ndarray): Shape (N, ), the predicted labels
                  of bounding boxes.
                - masks (list[RLE], optional): The predicted masks.
                - mask_scores (np.array, optional): Shape (N, ), the predicted
                  scores of masks.
        """
        for prediction in predictions:
            assert isinstance(prediction, dict), 'The prediciton should be ' \
                f'a sequence of dict, but got a sequence of {type(prediction)}.'  # noqa: E501
            self._results.append(prediction)

    def __call__(self, *args, **kwargs) -> Dict:
        """Stateless call for a metric compute."""

        # cache states
        cache_results = self._results
        cache_lvis_api = self._lvis_api
        cache_cat_ids = self.cat_ids
        cache_img_ids = self.img_ids

        self._results = []
        self.add(*args, **kwargs)
        metric_result = self.compute_metric(self._results)

        # recover states from cache
        self._results = cache_results
        self._lvis_api = cache_lvis_api
        self.cat_ids = cache_cat_ids
        self.img_ids = cache_img_ids

        return metric_result

    def compute_metric(  # type: ignore
            self, results: list) -> Dict[str, Union[float, list]]:
        """Compute the LVIS metrics.

        Args:
            results (List[tuple]): A list of tuple. Each tuple is the
                prediction and ground truth of an image. This list has already
                been synced across all ranks.

        Returns:
            dict: The computed metric. The keys are the names of
            the metrics, and the values are corresponding results.
        """
        tmp_dir = None
        if self.outfile_prefix is None:
            tmp_dir = tempfile.TemporaryDirectory()
            outfile_prefix = osp.join(tmp_dir.name, 'results')
        else:
            outfile_prefix = self.outfile_prefix

        # handle lazy init
        if len(self.cat_ids) == 0:
            self.cat_ids = self._lvis_api.get_cat_ids()
        if len(self.img_ids) == 0:
            self.img_ids = self._lvis_api.get_img_ids()

        # convert predictions to coco format and dump to json file
        result_files = self.results2json(results, outfile_prefix)

        eval_results: OrderedDict = OrderedDict()
        if self.format_only:
            self.logger.info('results are saved in '
                             f'{osp.dirname(outfile_prefix)}')
            return eval_results

        lvis_gt = self._lvis_api

        for metric in self.metrics:
            self.logger.info(f'Evaluating {metric}...')

            try:
                lvis_dt = LVISResults(lvis_gt, result_files[metric])
            except IndexError:
                self.logger.warning(
                    'The testing results of the whole dataset is empty.')
                break

            iou_type = 'bbox' if metric == 'proposal' else metric
            lvis_eval = LVISEval(lvis_gt, lvis_dt, iou_type)
            lvis_eval.params.imgIds = self.img_ids
            metric_items = self.metric_items
            if metric == 'proposal':
                lvis_eval.params.max_dets = self.proposal_nums
                lvis_eval.evaluate()
                lvis_eval.accumulate()
                lvis_eval.summarize()
                if metric_items is None:
                    metric_items = [
                        f'AR@{self.proposal_nums}',
                        f'ARs@{self.proposal_nums}',
                        f'ARm@{self.proposal_nums}',
                        f'ARl@{self.proposal_nums}'
                    ]
                for k, v in lvis_eval.get_results().items():
                    if k in metric_items:
                        val = float(f'{float(v):.3f}')
                        eval_results[k] = val

            else:
                lvis_eval.evaluate()
                lvis_eval.accumulate()
                lvis_eval.summarize()
                lvis_results = lvis_eval.get_results()

                if metric_items is None:
                    metric_items = [
                        'AP', 'AP50', 'AP75', 'APs', 'APm', 'APl', 'APr',
                        'APc', 'APf'
                    ]

                results_list = []
                for metric_item, v in lvis_results.items():
                    if metric_item in metric_items:
                        key = f'{metric}_{metric_item}'
                        val = float(v)
                        results_list.append(f'{round(val * 100, 2)}')
                        eval_results[key] = val
                eval_results[f'{metric}_result'] = results_list

                if self.classwise:  # Compute per-category AP
                    # Compute per-category AP
                    # from https://github.com/facebookresearch/detectron2/
                    precisions = lvis_eval.eval['precision']
                    # precision: (iou, recall, cls, area range, max dets)
                    assert len(self.cat_ids) == precisions.shape[2]

                    results_per_category = []
                    for idx, catId in enumerate(self.cat_ids):
                        # area range index 0: all area ranges
                        # max dets index -1: typically 100 per image
                        # the dimensions of precisions are
                        # [num_thrs, num_recalls, num_cats, num_area_rngs]
                        nm = self._lvis_api.load_cats([catId])[0]
                        precision = precisions[:, :, idx, 0]
                        precision = precision[precision > -1]
                        if precision.size:
                            ap = np.mean(precision)
                        else:
                            ap = float('nan')
                        results_per_category.append(
                            (f'{nm["name"]}', f'{round(ap * 100, 2)}'))
                        eval_results[f'{metric}_{nm["name"]}_precision'] = ap

                    eval_results[f'{metric}_classwise_result'] = \
                        results_per_category
            # Save lvis summarize print information to logger
            redirect_string = io.StringIO()
            with contextlib.redirect_stdout(redirect_string):
                lvis_eval.print_results()
            self.logger.info('\n' + redirect_string.getvalue())
        if tmp_dir is not None:
            tmp_dir.cleanup()
        return eval_results