import io
import json
import asyncio
import re
from difflib import SequenceMatcher
from typing import List, Dict, Any

import torch
import aiofiles
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

# Константы
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 МБ

# ---------------------------------------------------------------
# Вспомогательные функции для обработки текста
# ---------------------------------------------------------------
async def extract_text_from_file(file: UploadFile) -> str:
    """Извлекает текст из загруженного файла."""
    filename = file.filename.lower()
    content = await file.read()

    if len(content) == 0:
        raise HTTPException(400, "Файл пуст")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, f"Файл слишком большой (макс. {MAX_FILE_SIZE // (1024*1024)} МБ)")

    try:
        if filename.endswith('.pdf'):
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
        elif filename.endswith('.xlsx'):
            import pandas as pd
            df = pd.read_excel(io.BytesIO(content), engine='openpyxl')
            text = df.astype(str).apply(lambda x: ' '.join(x), axis=1).str.cat(sep='\n')
        elif filename.endswith('.xls'):
            import pandas as pd
            df = pd.read_excel(io.BytesIO(content), engine='xlrd')
            text = df.astype(str).apply(lambda x: ' '.join(x), axis=1).str.cat(sep='\n')
        elif filename.endswith('.docx'):
            from docx import Document
            doc = Document(io.BytesIO(content))
            text = '\n'.join(paragraph.text for paragraph in doc.paragraphs)
        elif filename.endswith('.json'):
            data = json.loads(content.decode('utf-8'))
            text = json.dumps(data, ensure_ascii=False, indent=2)
        elif filename.endswith('.txt'):
            text = content.decode('utf-8')
        else:
            raise HTTPException(400, f"Неподдерживаемый формат файла: {filename}")

        if not text or not text.strip():
            raise HTTPException(400, "Не удалось извлечь текст из файла")
        return text.strip()
    except Exception as e:
        raise HTTPException(500, f"Ошибка при извлечении текста: {str(e)}")


# ---------------------------------------------------------------
# Функции для анализа тендерной документации
# ---------------------------------------------------------------
def chunk_text(text: str, tokenizer, max_tokens=15000, overlap_tokens=1000) -> List[str]:
    """Разбивает текст на чанки с перекрытием."""
    tokens = tokenizer.encode(text, add_special_tokens=False)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = tokenizer.decode(chunk_tokens, skip_special_tokens=True)
        chunks.append(chunk_text)
        if end == len(tokens):
            break
        start = end - overlap_tokens
    return chunks


def deduplicate_requirements(requirements: List[str], threshold=0.85) -> List[str]:
    """Убирает дубликаты по коэффициенту схожести."""
    unique = []
    for req in requirements:
        if not any(SequenceMatcher(None, req, u).ratio() > threshold for u in unique):
            unique.append(req)
    return unique


def generate_text(model, tokenizer, prompt: str, max_new_tokens=4096) -> str:
    """Генерирует ответ модели (синхронно)."""
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=True,
            temperature=0.1,
            top_p=0.9,
            repetition_penalty=1.15,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.pad_token_id,
        )
    return tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)


def extract_tender_summary(model, tokenizer, text: str, max_new_tokens=512) -> Dict[str, str]:
    """
    Извлекает название и краткое описание тендера.
    Возвращает словарь: {"name": "...", "description": "..."}
    """
    system = (
        "Ты — эксперт по анализу закупочной документации. "
        "Извлеки полное официальное название тендера и его краткое описание (одно-два предложения). "
        "Верни строго JSON с ключами \"name\" и \"description\". Если название не найдено, верни пустую строку."
    )
    # Берём начало документа (первые ~2000 токенов)
    short_text = text[:8000]
    prompt = (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\nПроанализируй начало документа:\n{short_text}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
    response = generate_text(model, tokenizer, prompt, max_new_tokens)
    try:
        json_str = re.search(r'\{.*\}', response, re.DOTALL).group()
        data = json.loads(json_str)
        return {
            "name": data.get("name", "").strip(),
            "description": data.get("description", "").strip()
        }
    except:
        print("Ошибка извлечения summary тендера, возвращены пустые значения.")
        return {"name": "", "description": ""}


def extract_requirements_from_chunk(model, tokenizer, chunk_text: str) -> List[str]:
    """Извлекает все требования из одного чанка."""
    system = (
        "Ты — эксперт по анализу закупочной документации. "
        "Извлеки все требования к участникам и условиям закупки из данного фрагмента документа. "
        "Ответ должен быть строго в формате JSON с ключом \"all_requirements\" (массив строк)."
    )
    prompt = (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\nПроанализируй фрагмент:\n{chunk_text}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
    response = generate_text(model, tokenizer, prompt, max_new_tokens=4096)
    try:
        json_str = re.search(r'\{.*\}', response, re.DOTALL).group()
        data = json.loads(json_str)
        return data.get("all_requirements", [])
    except:
        return []


def extract_key_requirements(model, tokenizer, all_requirements: List[str]) -> List[str]:
    """Выделяет ключевые требования из полного списка."""
    system = (
        "Ты — эксперт по закупкам. Из приведённого списка требований выбери наиболее важные (ключевые), "
        "которые критичны для допуска к закупке или влияют на победу. "
        "Ответ должен быть строго в формате JSON с ключом \"key_requirements\" (массив строк)."
    )
    req_list = json.dumps(all_requirements, ensure_ascii=False, indent=2)
    prompt = (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\nСписок требований:\n{req_list}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
    response = generate_text(model, tokenizer, prompt, max_new_tokens=2048)
    try:
        json_str = re.search(r'\{.*\}', response, re.DOTALL).group()
        data = json.loads(json_str)
        return data.get("key_requirements", [])
    except:
        return []


def analyze_document(text: str, model, tokenizer) -> Dict[str, Any]:
    """Полный пайплайн обработки документа с извлечением summary, всех требований и ключевых требований."""
    # 1. Извлечение названия и описания
    tender_summary = extract_tender_summary(model, tokenizer, text)

    # 2. Разбиение на чанки и сбор всех требований
    chunks = chunk_text(text, tokenizer, max_tokens=15000, overlap_tokens=1000)
    all_reqs = []
    for chunk in chunks:
        reqs = extract_requirements_from_chunk(model, tokenizer, chunk)
        all_reqs.extend(reqs)

    # 3. Дедупликация требований
    all_reqs = deduplicate_requirements(all_reqs)

    # 4. Выделение ключевых требований
    key_reqs = extract_key_requirements(model, tokenizer, all_reqs)

    return {
        "tender_summary": tender_summary,
        "all_requirements": all_reqs,
        "key_requirements": key_reqs,
    }


# ---------------------------------------------------------------
# Модели ответа
# ---------------------------------------------------------------
class TenderSummary(BaseModel):
    name: str
    description: str


class TenderAnalysisResponse(BaseModel):
    status: str
    extracted_text_preview: str
    tender_summary: TenderSummary
    all_requirements: List[str]
    key_requirements: List[str]


# ---------------------------------------------------------------
# Роутер
# ---------------------------------------------------------------
router = APIRouter(prefix="/TenderAnalysis", tags=["Tender Analysis"])


@router.post("", response_model=TenderAnalysisResponse)
async def analyze_tender(
    request: Request,
    file: UploadFile = File(...)
):
    # Извлечение текста
    try:
        extracted_text = await extract_text_from_file(file)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка чтения файла: {str(e)}")

    # Получение модели и токенизатора из состояния приложения
    model = request.app.state.model
    tokenizer = request.app.state.tokenizer

    # Убедимся, что pad_token установлен
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Запуск анализа в отдельном потоке (не блокируем event loop)
    try:
        result = await asyncio.to_thread(analyze_document, extracted_text, model, tokenizer)
    except Exception as e:
        raise HTTPException(500, f"Ошибка анализа: {str(e)}")

    preview = extracted_text[:500] + "..." if len(extracted_text) > 500 else extracted_text

    return TenderAnalysisResponse(
        status="success",
        extracted_text_preview=preview,
        tender_summary=result["tender_summary"],
        all_requirements=result["all_requirements"],
        key_requirements=result["key_requirements"],
    )