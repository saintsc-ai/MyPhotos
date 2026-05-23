"""COCO 80-class labels in the order YOLOv8 emits.

`COCO_KO` maps the model's class index to a short Korean label that we
use as the auto-tag name. A few unhelpful classes (e.g. "potted plant",
"hair drier") are mapped but won't typically reach the confidence
threshold for ordinary photos.
"""

from __future__ import annotations

# Standard COCO 80 order (Ultralytics YOLOv8). DO NOT reorder.
COCO_EN: list[str] = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]

COCO_KO: dict[int, str] = {
    0: "사람", 1: "자전거", 2: "자동차", 3: "오토바이", 4: "비행기",
    5: "버스", 6: "기차", 7: "트럭", 8: "보트", 9: "신호등",
    10: "소화전", 11: "정지신호", 12: "주차요금기", 13: "벤치",
    14: "새", 15: "고양이", 16: "개", 17: "말", 18: "양", 19: "소",
    20: "코끼리", 21: "곰", 22: "얼룩말", 23: "기린",
    24: "배낭", 25: "우산", 26: "핸드백", 27: "넥타이", 28: "여행가방",
    29: "원반", 30: "스키", 31: "스노보드", 32: "공", 33: "연",
    34: "야구방망이", 35: "야구글러브", 36: "스케이트보드",
    37: "서핑보드", 38: "테니스라켓",
    39: "병", 40: "와인잔", 41: "컵", 42: "포크", 43: "칼",
    44: "숟가락", 45: "그릇",
    46: "바나나", 47: "사과", 48: "샌드위치", 49: "오렌지",
    50: "브로콜리", 51: "당근", 52: "핫도그", 53: "피자",
    54: "도넛", 55: "케이크",
    56: "의자", 57: "소파", 58: "화분", 59: "침대", 60: "식탁",
    61: "변기",
    62: "TV", 63: "노트북", 64: "마우스", 65: "리모컨",
    66: "키보드", 67: "휴대폰",
    68: "전자레인지", 69: "오븐", 70: "토스터", 71: "싱크대",
    72: "냉장고",
    73: "책", 74: "시계", 75: "꽃병", 76: "가위", 77: "곰인형",
    78: "헤어드라이어", 79: "칫솔",
}


def label_for(class_id: int) -> str:
    """Return Korean label if known, else the English fallback."""
    if class_id in COCO_KO:
        return COCO_KO[class_id]
    if 0 <= class_id < len(COCO_EN):
        return COCO_EN[class_id]
    return f"class-{class_id}"
