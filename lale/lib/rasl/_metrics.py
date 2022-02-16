# Copyright 2022 IBM Corporation
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

import functools
from abc import abstractmethod
from typing import Any, Dict, Iterable, Tuple

import numpy as np
import pandas as pd

from lale.datasets.data_schemas import add_table_name
from lale.expressions import astype, count, it, sum
from lale.helpers import _ensure_pandas
from lale.lib.dataframe import get_columns
from lale.operators import TrainableOperator

from ._monoid import Monoid, MonoidMaker
from .aggregate import Aggregate
from .map import Map

MetricMonoid = Monoid[float]

_Batch = Tuple[pd.Series, pd.Series]


class MetricMonoidMaker(MonoidMaker[_Batch, float]):
    @abstractmethod
    def to_monoid(self, v: _Batch) -> Monoid[float]:
        pass

    def score_data(self, y_true: pd.Series, y_pred: pd.Series) -> float:
        return (self.to_monoid((y_true, y_pred))).result

    def score_estimator(
        self, estimator: TrainableOperator, X: pd.DataFrame, y: pd.Series
    ) -> float:
        return self.score_data(y_true=y, y_pred=estimator.predict(X))

    def __call__(
        self, estimator: TrainableOperator, X: pd.DataFrame, y: pd.Series
    ) -> float:
        return self.score_estimator(estimator, X, y)

    def score_data_batched(
        self, batches: Iterable[Tuple[pd.Series, pd.Series]]
    ) -> float:
        lifted_batches = (self.to_monoid(b) for b in batches)
        return (functools.reduce(lambda x, y: x.combine(y), lifted_batches)).result

    def score_estimator_batched(
        self,
        estimator: TrainableOperator,
        batches: Iterable[Tuple[pd.Series, pd.Series]],
    ) -> float:
        predicted_batches = ((y, estimator.predict(X)) for X, y in batches)
        return self.score_data_batched(predicted_batches)


class _AccuracyData(MetricMonoid):
    def __init__(self, match, total):
        self._match = match
        self._total = total

    def combine(self, other):
        return _AccuracyData(self._match + other._match, self._total + other._total)

    @property
    def result(self):
        return self._match / np.float64(self._total)


class _Accuracy(MetricMonoidMaker):
    def __init__(self):
        from lale.lib.lale.concat_features import ConcatFeatures

        self._pipeline_suffix = (
            ConcatFeatures
            >> Map(columns={"match": astype("int", it.y_true == it.y_pred)})  # type: ignore
            >> Aggregate(columns={"match": sum(it.match), "total": count(it.match)})
        )

    def to_monoid(self, batch: _Batch):
        from lale.lib.rasl import Scan

        y_true, y_pred = batch
        assert isinstance(y_true, pd.Series), type(y_true)  # TODO: Spark
        if isinstance(y_pred, np.ndarray):
            y_pred = pd.Series(y_pred, y_true.index, y_true.dtype, "y_pred")
        assert isinstance(y_pred, pd.Series), type(y_pred)  # TODO: Spark
        y_true = add_table_name(pd.DataFrame(y_true), "y_true")
        y_pred = add_table_name(pd.DataFrame(y_pred), "y_pred")
        prefix_true = Scan(table=it.y_true) >> Map(
            columns={"y_true": it[get_columns(y_true)[0]]}
        )
        prefix_pred = Scan(table=it.y_pred) >> Map(
            columns={"y_pred": it[get_columns(y_pred)[0]]}
        )
        pipeline = (prefix_true & prefix_pred) >> self._pipeline_suffix
        agg_df = _ensure_pandas(pipeline.transform([y_true, y_pred]))
        return _AccuracyData(*agg_df.iloc[0])


def accuracy_score(y_true, y_pred):
    return get_scorer("accuracy").score_data(y_true, y_pred)


class _R2Data(MetricMonoid):
    def __init__(self, n, sum, sum_sq, res_sum_sq):
        self._n = n
        self._sum = sum
        self._sum_sq = sum_sq
        self._res_sum_sq = res_sum_sq

    def combine(self, other):
        return _R2Data(
            n=self._n + other._n,
            sum=self._sum + other._sum,
            sum_sq=self._sum_sq + other._sum_sq,
            res_sum_sq=self._res_sum_sq + other._res_sum_sq,
        )

    @property
    def result(self):
        ss_tot = self._sum_sq - (self._sum * self._sum / np.float64(self._n))
        return 1 - self._res_sum_sq / ss_tot


class _R2(MetricMonoidMaker):
    # https://en.wikipedia.org/wiki/Coefficient_of_determination

    def __init__(self):
        from lale.lib.lale.concat_features import ConcatFeatures

        self._pipeline_suffix = (
            ConcatFeatures
            >> Map(
                columns={
                    "y": it.y_true,  # observed values
                    "f": it.y_pred,  # predicted values
                    "y2": it.y_true * it.y_true,  # squares
                    "e2": (it.y_true - it.y_pred) * (it.y_true - it.y_pred),  # type: ignore
                }
            )
            >> Aggregate(
                columns={
                    "n": count(it.y),
                    "sum": sum(it.y),
                    "sum_sq": sum(it.y2),
                    "res_sum_sq": sum(it.e2),  # residual sum of squares
                }
            )
        )

    def to_monoid(self, batch):
        from lale.lib.rasl import Scan

        y_true, y_pred = batch
        assert isinstance(y_true, pd.Series), type(y_true)  # TODO: Spark
        if isinstance(y_pred, np.ndarray):
            y_pred = pd.Series(y_pred, y_true.index, y_true.dtype, "y_pred")
        assert isinstance(y_pred, pd.Series), type(y_pred)  # TODO: Spark
        y_true = add_table_name(pd.DataFrame(y_true), "y_true")
        y_pred = add_table_name(pd.DataFrame(y_pred), "y_pred")
        prefix_true = Scan(table=it.y_true) >> Map(
            columns={"y_true": it[get_columns(y_true)[0]]}
        )
        prefix_pred = Scan(table=it.y_pred) >> Map(
            columns={"y_pred": it[get_columns(y_pred)[0]]}
        )
        pipeline = (prefix_true & prefix_pred) >> self._pipeline_suffix
        agg_df = _ensure_pandas(pipeline.transform([y_true, y_pred]))
        return _R2Data(*agg_df.iloc[0])


def r2_score(y_true, y_pred):
    return get_scorer("r2").score_data(y_true, y_pred)


_scorer_cache: Dict[str, Any] = {"accuracy": None, "r2": None}


def get_scorer(scoring: str):
    assert scoring in _scorer_cache, scoring
    if _scorer_cache[scoring] is None:
        if scoring == "accuracy":
            _scorer_cache[scoring] = _Accuracy()
        elif scoring == "r2":
            _scorer_cache[scoring] = _R2()
    return _scorer_cache[scoring]