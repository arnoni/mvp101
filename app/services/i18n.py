TRANSLATIONS = {
    "en": {
        "title": "DillDrill",
        "subtitle": "Detect noisy rentals ahead of time – plan visits only for quiet homes.",
        "input_label": "Enter Latitude, Longitude",
        "placeholder": "e.g., 16.061, 108.235",
        "button_search": "Find Nearest Projects",
        "button_searching": "Searching...",
        "results_header": "Nearest Projects",
        "go_there": "Go There",
        "distance_unit": "m away",
        "error_prefix": "Error",
        "success_prefix": "Success",
        "lang_en": "English",
        "lang_es": "Español", 
        "lang_ru": "Русский",
        "lang_ko": "한국어"
    },
    "es": {
        "title": "DillDrill",
        "subtitle": "Detecte alquileres ruidosos con antelación.",
        "input_label": "Ingrese Latitud, Longitud",
        "placeholder": "ej., 16.061, 108.235",
        "button_search": "Buscar Proyectos",
        "button_searching": "Buscando...",
        "results_header": "Proyectos Cercanos",
        "go_there": "Ir Allí",
        "distance_unit": "m de distancia",
        "error_prefix": "Error",
        "success_prefix": "Éxito",
        "lang_en": "English",
        "lang_es": "Español", 
        "lang_ru": "Русский",
        "lang_ko": "한국어"
    },
    "ru": {
        "title": "DillDrill",
        "subtitle": "Обнаруживайте шумное жилье заранее.",
        "input_label": "Введите широту, долготу",
        "placeholder": "напр., 16.061, 108.235",
        "button_search": "Найти проекты",
        "button_searching": "Поиск...",
        "results_header": "Ближайшие проекты",
        "go_there": "Маршрут",
        "distance_unit": "м от вас",
        "error_prefix": "Ошибка",
        "success_prefix": "Успех",
        "lang_en": "English",
        "lang_es": "Español", 
        "lang_ru": "Русский",
        "lang_ko": "한국어"
    },
    "ko": {
        "title": "DillDrill",
        "subtitle": "시끄러운 임대 숙소를 미리 감지하세요.",
        "input_label": "위도, 경도 입력",
        "placeholder": "예: 16.061, 108.235",
        "button_search": "가까운 프로젝트 찾기",
        "button_searching": "검색 중...",
        "results_header": "가까운 프로젝트",
        "go_there": "가기",
        "distance_unit": "m 거리",
        "error_prefix": "오류",
        "success_prefix": "성공",
        "lang_en": "English",
        "lang_es": "Español", 
        "lang_ru": "Русский",
        "lang_ko": "한국어"
    }
}

def get_translations(lang: str = "en") -> dict:
    # Basic fallback
    if lang not in TRANSLATIONS:
        lang = "en"
    return TRANSLATIONS[lang]
