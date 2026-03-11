from dataclasses import dataclass, field
from typing import Optional, List
import uuid
from datetime import datetime


@dataclass
class TextProcessingRequest:
    """
    文本处理请求数据结构
    
    Attributes:
        text: 原始文本内容
        offset: 偏移量，用于加密/解密操作
        request_id: 请求唯一标识符
        timestamp: 请求时间戳
    """
    text: str
    offset: int = 0
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class TextProcessingResult:
    """
    文本处理结果数据结构
    
    Attributes:
        original_text: 原始文本内容
        processed_text: 处理后的文本内容（加密或解密结果）
        offset: 使用的偏移量
        success: 处理是否成功
        error_message: 错误信息（如果处理失败）
        request_id: 对应的请求ID
        timestamp: 结果生成时间戳
    """
    original_text: str
    processed_text: str
    offset: int
    success: bool
    error_message: Optional[str] = None
    request_id: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        """验证必要字段"""
        if self.original_text is None:
            raise ValueError("original_text cannot be None")
        if self.processed_text is None:
            raise ValueError("processed_text cannot be None")
        if not isinstance(self.offset, int):
            raise TypeError("offset must be an integer")


@dataclass
class BatchTextProcessingRequest:
    """
    批量文本处理请求数据结构
    
    Attributes:
        requests: 文本处理请求列表
        batch_id: 批处理唯一标识符
        timestamp: 批处理创建时间戳
    """
    requests: List[TextProcessingRequest]
    batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        """验证请求列表"""
        if self.requests is None:
            raise ValueError("requests cannot be None")
        if not isinstance(self.requests, list):
            raise TypeError("requests must be a list of TextProcessingRequest objects")


@dataclass
class BatchTextProcessingResult:
    """
    批量文本处理结果数据结构
    
    Attributes:
        results: 文本处理结果列表
        batch_id: 对应的批处理ID
        total_count: 总处理数量
        success_count: 成功处理数量
        failed_count: 失败处理数量
        timestamp: 结果生成时间戳
    """
    results: List[TextProcessingResult]
    batch_id: str
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        """计算统计信息"""
        if self.results is None:
            raise ValueError("results cannot be None")
        if not isinstance(self.results, list):
            raise TypeError("results must be a list of TextProcessingResult objects")
        
        self.total_count = len(self.results)
        self.success_count = sum(1 for result in self.results if result.success)
        self.failed_count = self.total_count - self.success_count
        if self.batch_id is None:
            raise ValueError("batch_id cannot be None")