"""RAG Service for DeepSeek integration."""

from __future__ import annotations

import logging
import os
import uuid

from openai import AsyncOpenAI

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except ImportError:
    RecursiveCharacterTextSplitter = None

try:
    from langchain_community.document_loaders import TextLoader, UnstructuredWordDocumentLoader
except ImportError:
    TextLoader = None
    UnstructuredWordDocumentLoader = None

try:
    import chromadb
    from chromadb.utils import embedding_functions
except ImportError:
    chromadb = None
    embedding_functions = None

from bot.config import settings
from bot.database import async_session
from bot.models import BotSettings, RAGConfig

logger = logging.getLogger(__name__)

# Single chromadb client instance for the bot
try:
    if chromadb is None or embedding_functions is None:
        raise RuntimeError("RAG dependencies are not installed")

    chroma_client = chromadb.PersistentClient(path="./chroma_db")
    # Using a fast, local embedding model from sentence-transformers
    sentence_transformer_ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    collection = chroma_client.get_or_create_collection(
        name="vpn_knowledge_base", embedding_function=sentence_transformer_ef
    )
except Exception as e:
    logger.error(f"Failed to initialize ChromaDB: {e}")
    collection = None

# OpenAI client for DeepSeek (DeepSeek API is OpenAI-compatible)
deepseek_client = AsyncOpenAI(
    api_key=settings.deepseek_api_key or "DUMMY",
    base_url="https://api.deepseek.com",
)


def _build_runtime_support_context(daily_charge_rub: float | None) -> str:
    lines = [
        "## Актуальные правила оплаты и баланса",
        "- У пользователя могут быть два сценария: купить тариф или пополнить баланс.",
        "- Баланс можно пополнить на любую сумму.",
        "- Ежедневные списания относятся только к базовому доступу.",
        "- Одна ссылка работает на 3 устройства.",
        "- Дополнительные устройства покупаются отдельно как слот и не входят в ежедневное списание.",
        "- Если пользователь выключил ежедневные списания, доступ не пропадает сразу, а остаётся до уже оплаченной даты.",
        "- Бонусы за приглашения тоже падают на баланс и могут тратиться на услуги.",
        "- Пополнять баланс и оплачивать можно в любое время суток.",
        "- Автоматическое ежедневное списание происходит в 05:00 по Москве.",
        "- В боте пополнение баланса открывается отдельной кнопкой «💰 Пополнить баланс» в главном меню.",
    ]
    if daily_charge_rub and daily_charge_rub > 0:
        lines.append(f"- Текущая дневная ставка у этого бота: {daily_charge_rub:.2f} ₽ в день.")
    else:
        lines.append("- Если точная дневная ставка не видна в контексте, не выдумывай сумму и направляй пользователя в «Пополнить баланс» или «Мой профиль».")

    if settings.webstore_public_enabled:
        lines.append(
            f"- У этого бота можно также использовать сайт {settings.webstore_api_base_url}, "
            "если вопрос связан с веб-покупкой или веб-профилем."
        )
    else:
        lines.append("- Не предлагай сайт и веб-профиль как основной сценарий, если пользователь сам про сайт не спрашивал.")

    lines.extend([
        "",
        "## Как отвечать про баланс",
        "- Объясняй просто, по-человечески, без технарского языка.",
        "- Сначала отвечай прямо на вопрос пользователя одним коротким абзацем.",
        "- Не уводи ответ в общий рассказ про сервис, если человек спросил о конкретной вещи.",
        "- Если вопрос был про время оплаты или пополнения, отвечай прямо: пополнять можно в любое время, а ежедневное списание идёт в 05:00 МСК.",
        "- Если вопрос был не про подключение, не начинай заново объяснять, как работает приложение и куда вставлять ссылку.",
        "- Если человек спрашивает, как считается списание по дням, объясняй только текущие правила этого бота.",
        "- Не придумывай общие цены для всех ботов: у каждого инстанса ставка может быть своей.",
        "- Не добавляй в конце пустые фразы вроде 'если будут вопросы — обращайтесь' без необходимости.",
    ])
    return "\n".join(lines)


async def process_document(file_path: str, filename: str) -> int:
    """Load a document, chunk it, and add to ChromaDB."""
    if not collection:
        raise RuntimeError("ChromaDB is not initialized.")
    if RecursiveCharacterTextSplitter is None:
        raise RuntimeError("langchain-text-splitters is not installed.")
    if TextLoader is None or UnstructuredWordDocumentLoader is None:
        raise RuntimeError("langchain-community is not installed.")

    ext = os.path.splitext(filename)[1].lower()

    if ext == ".txt":
        loader = TextLoader(file_path, encoding="utf-8")
    elif ext in [".doc", ".docx"]:
        loader = UnstructuredWordDocumentLoader(file_path)
    else:
        raise ValueError(f"Unsupported file extension: {ext}")

    docs = loader.load()

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        length_function=len,
    )
    chunks = text_splitter.split_documents(docs)

    docs_texts = []
    ids = []
    metadatas = []

    for chunk in chunks:
        chunk_id = f"{filename}_{uuid.uuid4().hex[:8]}"
        docs_texts.append(chunk.page_content)
        ids.append(chunk_id)
        metadatas.append({"source": filename})

    if docs_texts:
        collection.add(
            documents=docs_texts,
            metadatas=metadatas,
            ids=ids
        )
        logger.info(f"Added {len(chunks)} chunks from {filename} to Chroma.")

    return len(chunks)


async def ask_deepseek(
    question: str,
    history: list[dict] | None = None,
    user_context: str | None = None,
) -> str:
    """Query DeepSeek with relevant context from ChromaDB and conversation history."""
    if not settings.deepseek_api_key:
        return "В данный момент AI-помощник недоступен (не настроен API ключ)."

    # 1. Retrieve RAG context
    context_text = ""
    if collection:
        try:
            results = collection.query(
                query_texts=[question],
                n_results=3
            )
            if results and results["documents"] and results["documents"][0]:
                context_text = "\n\n".join(results["documents"][0])
        except Exception as e:
            logger.error(f"Failed to query ChromaDB: {e}")

    # 2. Get AI config from DB
    async with async_session() as session:
        rag_config = await session.get(RAGConfig, 1)
        if not rag_config:
            rag_config = RAGConfig(
                system_prompt="Ты — помощник службы поддержки сервиса ускорения интернета. Помогай пользователям.",
                temperature=0.7
            )
            session.add(rag_config)
            await session.commit()

        system_prompt = rag_config.system_prompt
        temperature = rag_config.temperature
        daily_charge_row = await session.get(BotSettings, "daily_charge_rub")
        daily_charge_rub = None
        if daily_charge_row and daily_charge_row.value:
            try:
                daily_charge_rub = round(float(daily_charge_row.value), 2)
            except (TypeError, ValueError):
                daily_charge_rub = None

    # 3. Construct messages: system + history + new question
    system_prompt += "\n\n" + _build_runtime_support_context(daily_charge_rub)
    if user_context:
        system_prompt += f"\n\n## Профиль текущего пользователя\n{user_context}"
    if context_text:
        system_prompt += f"\n\nКонтекст из базы знаний (используй если релевантно):\n{context_text}"

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    try:
        response = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=temperature,
            max_tokens=1500,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"DeepSeek API error: {e}")
        return "Извините, произошла ошибка при обращении к ИИ. Попробуйте немного позже."
