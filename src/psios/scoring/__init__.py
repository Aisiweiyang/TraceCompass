"""评分纯函数层(inference-design §2):无 IO,全部可表驱动测试。"""

from psios.scoring.interest import (
    InterestComponents,
    TopicSignals,
    compute_components,
    confidence_of,
    interest_type_of,
    lifecycle_of,
    weighted_score,
)

__all__ = [
    "InterestComponents",
    "TopicSignals",
    "compute_components",
    "confidence_of",
    "interest_type_of",
    "lifecycle_of",
    "weighted_score",
]
