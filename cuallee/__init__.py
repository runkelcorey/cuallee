import enum
import hashlib
import operator
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from typing import Any, Callable, List, Optional, Tuple, Union, Dict

import pyspark.sql.functions as F
import pyspark.sql.types as T
from pyspark.sql import DataFrame, Observation, SparkSession, Column, Row
from toolz import compose, valfilter

from . import dataframe as D
from . import exceptions as E


class CheckLevel(enum.Enum):
    WARNING = 0
    ERROR = 1


class CheckDataType(enum.Enum):
    AGNOSTIC = 0
    NUMERIC = 1
    STRING = 2
    DATE = 3
    TIMESTAMP = 4


@dataclass(frozen=True)
class Rule:
    method: str
    column: Union[Tuple, str]
    value: Optional[Any]
    data_type: CheckDataType
    coverage: float = 1.0

    def __repr__(self):
        return f"Rule(method:{self.method}, column:{self.column}, value:{self.value}, data_type:{self.data_type}, coverage:{self.coverage}"


@dataclass(frozen=True)
class ComputeInstruction:
    rule: Rule
    expression: Column


class Check:
    def __init__(
        self, level: CheckLevel, name: str, execution_date: datetime = datetime.today()
    ):
        self._compute: Dict[str, ComputeInstruction] = {}
        self._unique: Dict[str, ComputeInstruction] = {}
        self.level = level
        self.name = name
        self.date = execution_date
        self.rows = None

    def __repr__(self):
        return (
            f"Check(level:{self.level}, desc:{self.name}, rules:{len(self._compute)})"
        )

    def _generate_rule_key_id(
        self,
        method: str,
        column: Union[Tuple, str],
        value: Any,
        coverage: float,
    ):
        return hashlib.blake2s(
            bytes(f"{method}{column}{value}{coverage}", "utf-8")
        ).hexdigest()

    def _single_value_rule(
        self,
        column: str,
        value: Optional[Any],
        operator: Callable,
    ):
        return F.sum((operator(F.col(column), value)).cast("integer"))

    def _integrate_compute(self) -> Dict:
        """Unifies the compute dictionaries from observation and select forms"""
        return {**self._unique, **self._compute}

    @staticmethod
    def _compute_columns(columns : Union[str, List[str]]) -> List[str]:
        """Confirm that all compute columns exists in dataframe"""
        def _normalize_columns(col : Union[str, List[str]], agg : List[str]) -> List[str]:
            """Recursive consilidation of compute columns"""
            if isinstance(col, str):
                agg.append(col)
            else:
                [_normalize_columns(inner_col, agg) for inner_col in col]
            return agg
        _column = compose(operator.attrgetter("column"), operator.attrgetter("rule"))
        return _normalize_columns(map(_column, columns), [])
        
    def _discriminate_result_type(self, column: Column) -> Column:
        """Function to convert categorical rule results into a continuous feature"""
        return (
            F.when(column.eqNullSafe("false"), F.lit(0.0))
            .when(column.eqNullSafe("true"), F.lit(1.0))
            .otherwise(column.cast(T.DoubleType()) / self.rows)
        )

    def _evaluate_status(self, pass_rate: str, pass_threshold: str) -> Column:
        """Assign the PASS/FAIL status to the threshold of the rules"""
        return F.when(pass_rate >= pass_threshold, F.lit("PASS")).otherwise(
            F.lit("FAIL")
        )

    def is_complete(self, column: str, pct: float = 1.0):
        """Validation for non-null values in column"""
        key = self._generate_rule_key_id("is_complete", column, "N/A", pct)
        self._compute[key] = ComputeInstruction(
            Rule("is_complete", column, "N/A", CheckDataType.AGNOSTIC, pct),
            F.sum(F.col(column).isNotNull().cast("integer")),
        )
        return self

    def are_complete(self, column: str, pct: float = 1.0):
        """Validation for non-null values in a group of columns"""
        # if isinstance(column, List):
        #    column = tuple(column)
        key = self._generate_rule_key_id("are_complete", column, "N/A", pct)
        self._compute[key] = ComputeInstruction(
            Rule("are_complete", column, "N/A", CheckDataType.AGNOSTIC, pct),
            reduce(
                operator.add,
                [F.sum(F.col(f"`{c}`").isNotNull().cast("integer")) for c in column],
            )
            / len(column),
        )
        return self

    def is_unique(self, column: str, pct: float = 1.0):
        """Validation for unique values in column"""
        key = self._generate_rule_key_id("is_unique", column, "N/A", pct)
        self._unique[key] = ComputeInstruction(
            Rule("is_unique", column, "N/A", CheckDataType.AGNOSTIC, pct),
            F.count_distinct(F.col(column)),
        )
        return self

    def are_unique(self, column: Tuple[str], pct: float = 1.0):
        """Validation for unique values in a group of columns"""
        if isinstance(column, List):
            column = tuple(column)
        key = self._generate_rule_key_id("are_unique", column, "N/A", pct)
        self._unique[key] = ComputeInstruction(
            Rule("are_unique", column, "N/A", CheckDataType.AGNOSTIC, pct),
            F.count_distinct(*[F.col(c) for c in column]),
        )
        return self

    def is_greater_than(self, column: str, value: float, pct: float = 1.0):
        """Validation for numeric greater than value"""
        key = self._generate_rule_key_id("is_greater_than", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("is_greater_than", column, value, CheckDataType.NUMERIC, pct),
            self._single_value_rule(column, value, operator.gt),
        )
        return self

    def is_greater_or_equal_than(self, column: str, value: float, pct: float = 1.0):
        """Validation for numeric greater or equal than value"""
        key = self._generate_rule_key_id("is_greater_or_equal_than", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("is_greater_or_equal_than", column, value, CheckDataType.NUMERIC, pct),
            self._single_value_rule(column, value, operator.ge),
        )
        return self

    def is_less_than(self, column: str, value: float, pct: float = 1.0):
        """Validation for numeric less than value"""
        key = self._generate_rule_key_id("is_less_than", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("is_less_than", column, value, CheckDataType.NUMERIC, pct),
            self._single_value_rule(column, value, operator.lt),
        )
        return self

    def is_less_or_equal_than(self, column: str, value: float, pct: float = 1.0):
        """Validation for numeric less or equal than value"""
        key = self._generate_rule_key_id("is_less_or_equal_than", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("is_less_or_equal_than", column, value, CheckDataType.NUMERIC, pct),
            self._single_value_rule(column, value, operator.le),
        )
        return self

    def is_equal_than(self, column: str, value: float, pct: float = 1.0):
        """Validation for numeric column equal than value"""
        key = self._generate_rule_key_id("is_equal", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("is_equal", column, value, CheckDataType.NUMERIC, pct),
            self._single_value_rule(column, value, operator.eq),
        )
        return self

    def matches_regex(self, column: str, value: str, pct: float = 1.0):
        """Validation for string type column matching regex expression"""
        key = self._generate_rule_key_id("matches_regex", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("matches_regex", column, value, CheckDataType.STRING, pct),
            F.sum((F.length(F.regexp_extract(column, value, 0)) > 0).cast("integer")),
        )
        return self

    def has_min(self, column: str, value: float, pct: float = 1.0):
        """Validation of a column’s minimum value"""
        key = self._generate_rule_key_id("has_min", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("has_min", column, value, CheckDataType.NUMERIC),
            F.min(F.col(column)) == value,
        )
        return self

    def has_max(self, column: str, value: float, pct: float = 1.0):
        """Validation of a column’s maximum value"""
        key = self._generate_rule_key_id("has_max", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("has_max", column, value, CheckDataType.NUMERIC),
            F.max(F.col(column)) == value,
        )
        return self

    def has_std(self, column: str, value: float, pct: float = 1.0):
        """Validation of a column’s standard deviation"""
        key = self._generate_rule_key_id("has_std", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("has_std", column, value, CheckDataType.NUMERIC),
            F.stddev_pop(F.col(column)) == value,
        )
        return self

    def has_mean(self, column: str, value: float, pct: float = 1.0):
        """Validation of a column's average/mean"""
        key = self._generate_rule_key_id("has_mean", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("has_mean", column, value, CheckDataType.NUMERIC),
            F.mean(F.col(f"`{column}`")).eqNullSafe(value),
        )
        return self

    def is_between(self, column: str, value: Tuple[Any], pct: float = 1.0):
        """Validation of a column between a range"""

        # Create tuple if user pass list
        if isinstance(value, List):
            value = tuple(value)

        key = self._generate_rule_key_id("is_between", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("is_between", column, value, CheckDataType.AGNOSTIC, pct),
            F.sum(F.col(column).between(*value).cast("integer")),  # type: ignore
        )
        return self

    def is_contained_in(
        self, column: str, value: Tuple[str, int, float], pct: float = 1.0
    ):
        """Validation of column value in set of given values"""
        # Create tuple if user pass list
        if isinstance(value, List):
            value = tuple(value)

        # Check value type to later assess correct column type
        if [isinstance(v, str) for v in value]:
            check = CheckDataType.STRING
        else:
            check = CheckDataType.NUMERIC

        key = self._generate_rule_key_id("is_contained_in", column, value, pct)
        self._compute[key] = ComputeInstruction(
            Rule("is_contained_in", column, value, check),
            F.sum((F.col(column).isin(list(value))).cast("integer")),
        )
        return self

    def has_percentile(
        self,
        column: str,
        value: float,
        percentile: float,
        precision: int = 10000,
        pct: float = 1.0,
    ):
        """Validation of a column percentile value"""
        key = self._generate_rule_key_id(
            "has_percentile", column, (value, percentile, precision), pct
        )
        self._unique[key] = ComputeInstruction(
            Rule(
                "has_percentile",
                column,
                (value, percentile, precision),
                CheckDataType.NUMERIC,
                pct,
            ),
            F.percentile_approx(F.col(f"`{column}`").cast(T.DoubleType()), percentile, precision).eqNullSafe(value),
        )
        return self

    def has_max_by(
        self, column_source: str, column_target: str, value: float, pct: float = 1.0
    ):
        """Validation of a column maximum based on other column maximum"""
        key = self._generate_rule_key_id(
            "has_max_by", (column_source, column_target), value, pct
        )
        self._compute[key] = ComputeInstruction(
            Rule(
                "has_max_by",
                (column_source, column_target),
                value,
                CheckDataType.NUMERIC,
            ),
            F.max_by(column_target, column_source) == value,
        )
        return self

    def has_min_by(
        self, column_source: str, column_target: str, value: float, pct: float = 1.0
    ):
        """Validation of a column minimum based on other column minimum"""
        key = self._generate_rule_key_id(
            "has_min_by", (column_source, column_target), value, pct
        )
        self._compute[key] = ComputeInstruction(
            Rule(
                "has_min_by",
                (column_source, column_target),
                value,
                CheckDataType.NUMERIC,
            ),
            F.min_by(column_target, column_source) == value,
        )
        return self

    def has_correlation(self, column_left: str, column_right: str, value: float, pct: float = 1.0):
        """ Validates the correlation between 2 columns with some tolerance"""
        
        key = self._generate_rule_key_id(
            "has_correlation", (column_left, column_right), value, pct
        )
        self._compute[key] = ComputeInstruction(
            Rule(
                "has_correlation",
                (column_left, column_right),
                value,
                CheckDataType.NUMERIC,
            ),
            F.corr(F.col(f"`{column_left}`"), F.col(f"`{column_right}`")).eqNullSafe(value),
        )
        return self

    def satisfies(self, predicate: str, pct: float = 1.0):
        """Validation of a column satisfying a SQL-like predicate"""
        key = self._generate_rule_key_id("satisfies", "N/A", predicate, pct)
        self._compute[key] = ComputeInstruction(
            Rule("satisfies", "N/A", predicate, CheckDataType.AGNOSTIC),
            F.sum(F.expr(predicate).cast("integer")),
        )
        return self

    def validate(self, spark: SparkSession, dataframe: DataFrame):
        """Compute all rules in this check for specific data frame"""

        # Merge `unique` and `compute` dict
        unified_rules = self._integrate_compute()
        rule_expressions = unified_rules.values()
        # Check the dictionnary is not empty
        assert unified_rules, "Check is empty. Add validations i.e. is_complete, is_unique, etc."

        # Check dataframe is spark dataframe
        assert isinstance(
            dataframe, DataFrame
        ), "Cualle operates only with Spark Dataframes"

        # Pre-validate column names
        column_set = set(Check._compute_columns(rule_expressions))
        unknown_columns = column_set.difference(set(dataframe.columns))
        assert not unknown_columns, f"Column(s): {unknown_columns} not in dataframe"

        # Pre-Validation of numeric data types
        _col = compose(operator.attrgetter("column"), operator.attrgetter("rule"))
        _numeric = lambda x: x.rule.data_type == CheckDataType.NUMERIC
        _date = lambda x: x.rule.data_type == CheckDataType.DATE
        _timestamp = lambda x: x.rule.data_type == CheckDataType.TIMESTAMP
        _string = lambda x: x.rule.data_type == CheckDataType.STRING
        assert set(map(_col, valfilter(_numeric, unified_rules).values())).issubset(D.numeric_fields(dataframe))
        assert set(map(_col, valfilter(_string, unified_rules).values())).issubset(D.string_fields(dataframe))
        assert set(map(_col, valfilter(_date, unified_rules).values())).issubset(D.date_fields(dataframe))
        assert set(map(_col, valfilter(_timestamp, unified_rules).values())).issubset(D.timestamp_fields(dataframe))
        
        if self._compute:
            observation = Observation(self.name)

            df_observation = dataframe.observe(
                observation,
                *[
                    compute_instruction.expression.alias(hash_key)
                    for hash_key, compute_instruction in self._compute.items()
                ]
            )
            rows = df_observation.count()
            observation_result = observation.get
        else: 
            observation_result = {}
            rows = dataframe.count()
        
        self.rows = rows

        unique_observe = (
            dataframe.select(
                *[
                    compute_instrunction.expression.alias(hash_key)
                    for hash_key, compute_instrunction in self._unique.items()
                ]
            )
            .first()
            .asDict()  # type: ignore
        )

        unified_results = {**unique_observe, **observation_result}

        return (
            spark.createDataFrame(
                [
                    Row(
                        index,
                        compute_instruction.rule.method,
                        str(compute_instruction.rule.column),
                        str(compute_instruction.rule.value),
                        unified_results[hash_key],
                        compute_instruction.rule.coverage,
                    )
                    for index, (hash_key, compute_instruction) in enumerate(
                        unified_rules.items(), 1
                    )
                ],
                schema="id int, rule string, column string, value string, positive_predicate string, pass_threshold string",
            )
            .select(
                F.col("id"),
                F.lit(self.date.strftime("%Y-%m-%d")).alias("date"),
                F.lit(self.date.strftime("%H:%M:%S")).alias("time"),
                F.lit(self.name).alias("check"),
                F.lit(self.level.name).alias("level"),
                F.col("column"),
                F.col("rule"),
                F.col("value"),
                F.lit(rows).alias("rows"),
                self._discriminate_result_type(F.col("positive_predicate")).alias(
                    "pass_rate"
                ),
                F.col("pass_threshold"),
            )
            .withColumn(
                "status",
                self._evaluate_status(F.col("pass_rate"), F.col("pass_threshold")),
            )
        )

