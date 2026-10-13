import os
import sys
import json
import time
import uuid
import io
from datetime import datetime
from typing import List, Dict, Any, Optional

if getattr(sys, "frozen", False):
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

KNOWLEDGE_DIR = os.path.join(DATA_DIR, "knowledge")
DOCS_DIR = os.path.join(KNOWLEDGE_DIR, "docs")
METADATA_FILE = os.path.join(KNOWLEDGE_DIR, "documents.json")

os.makedirs(DOCS_DIR, exist_ok=True)


def load_documents_metadata() -> List[Dict[str, Any]]:
    """Loads all uploaded document metadata records from documents.json."""
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for doc in data:
                        if "file_size" in doc and "size_bytes" not in doc:
                            doc["size_bytes"] = doc["file_size"]
                        elif "size_bytes" in doc and "file_size" not in doc:
                            doc["file_size"] = doc["size_bytes"]
                    return data
        except Exception as e:
            print(f"[Knowledge Error] Could not read metadata: {e}")
    return []


def save_documents_metadata(docs: List[Dict[str, Any]]) -> bool:
    """Saves the document metadata records to documents.json."""
    try:
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        with open(METADATA_FILE, "w", encoding="utf-8") as f:
            json.dump(docs, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[Knowledge Error] Could not write metadata: {e}")
        return False


def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """
    Extracts readable text from PDF, DOCX, TXT, MD, CSV, or JSON file bytes.
    """
    ext = os.path.splitext(filename)[1].lower()
    text = ""

    if ext == ".pdf":
        try:
            import PyPDF2
            stream = io.BytesIO(file_bytes)
            reader = PyPDF2.PdfReader(stream)
            extracted_pages = []
            for idx, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text and page_text.strip():
                    extracted_pages.append(f"--- [Page {idx + 1}] ---\n{page_text.strip()}")
            text = "\n\n".join(extracted_pages)
        except Exception as e:
            print(f"[Knowledge Parser] PyPDF2 extraction error on {filename}: {e}")
            text = f"[Error extracting text from PDF: {e}]"

    elif ext == ".docx":
        try:
            import docx
            stream = io.BytesIO(file_bytes)
            doc = docx.Document(stream)
            parts = []
            for p in doc.paragraphs:
                p_text = p.text.strip()
                if p_text:
                    parts.append(p_text)
            for t_idx, table in enumerate(doc.tables):
                parts.append(f"\n[Table {t_idx + 1}]")
                for row in table.rows:
                    row_cells = [c.text.strip() for c in row.cells]
                    parts.append(" | ".join(row_cells))
            text = "\n".join(parts)
        except Exception as e:
            print(f"[Knowledge Parser] python-docx extraction error on {filename}: {e}")
            text = f"[Error extracting text from DOCX: {e}]"

    elif ext in [".txt", ".md", ".csv", ".json"]:
        for enc in ["utf-8", "utf-8-sig", "cp1252", "latin-1"]:
            try:
                text = file_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            text = file_bytes.decode("utf-8", errors="ignore")

    else:
        # Generic text attempt for unrecognized extensions
        try:
            text = file_bytes.decode("utf-8", errors="ignore")
        except Exception:
            text = ""

    return text.strip()


def add_document(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """
    Saves an uploaded business document, parses its text, and stores its metadata.
    """
    clean_name = os.path.basename(filename)
    ext = os.path.splitext(clean_name)[1].lower()
    doc_id = f"doc_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    saved_filename = f"{doc_id}_{clean_name}"
    saved_path = os.path.join(DOCS_DIR, saved_filename)

    with open(saved_path, "wb") as f:
        f.write(file_bytes)

    extracted_text = extract_text_from_file(file_bytes, clean_name)
    words = len(extracted_text.split())
    chars = len(extracted_text)
    preview = extracted_text[:350] + ("..." if len(extracted_text) > 350 else "")

    new_doc = {
        "id": doc_id,
        "filename": clean_name,
        "saved_filename": saved_filename,
        "file_type": ext.lstrip("."),
        "file_size": len(file_bytes),
        "size_bytes": len(file_bytes),
        "uploaded_at": datetime.now().isoformat(),
        "word_count": words,
        "char_count": chars,
        "enabled": True,
        "preview": preview,
        "text": extracted_text
    }

    docs = load_documents_metadata()
    docs.insert(0, new_doc)
    save_documents_metadata(docs)
    print(f"[Knowledge Base] Successfully added '{clean_name}' ({words} words extracted).")
    return new_doc


def delete_document(doc_id: str) -> bool:
    """Deletes a document by ID and removes its file from disk."""
    docs = load_documents_metadata()
    found = None
    remaining = []
    for d in docs:
        if d.get("id") == doc_id:
            found = d
        else:
            remaining.append(d)

    if not found:
        return False

    saved_file = found.get("saved_filename")
    if saved_file:
        path = os.path.join(DOCS_DIR, saved_file)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"[Knowledge Warning] Could not remove disk file {path}: {e}")

    save_documents_metadata(remaining)
    print(f"[Knowledge Base] Deleted document #{doc_id} ('{found.get('filename')}').")
    return True


def toggle_document(doc_id: str, enabled: Optional[bool] = None) -> Optional[bool]:
    """Toggles whether an uploaded document is active in the AI's live call prompt."""
    docs = load_documents_metadata()
    updated = False
    new_state = None
    for d in docs:
        if d.get("id") == doc_id:
            if enabled is None:
                d["enabled"] = not d.get("enabled", True)
            else:
                d["enabled"] = bool(enabled)
            new_state = d["enabled"]
            updated = True
            break

    if updated:
        save_documents_metadata(docs)
        return new_state
    return None


def get_all_documents() -> List[Dict[str, Any]]:
    """Returns list of documents with metadata and brief preview for UI display."""
    docs = load_documents_metadata()
    summary_list = []
    for d in docs:
        item = dict(d)
        if "text" in item and len(item["text"]) > 400:
            item["text_snippet"] = item["text"][:400] + "..."
        else:
            item["text_snippet"] = item.get("text", "")
        # Don't send huge raw text in standard listing to keep JSON lightweight
        if len(item.get("text", "")) > 1000:
            item["has_full_text"] = True
            del item["text"]
        summary_list.append(item)
    return summary_list


def get_document_full(doc_id: str) -> Optional[Dict[str, Any]]:
    """Returns complete document including full extracted text for modal preview."""
    docs = load_documents_metadata()
    for d in docs:
        if d.get("id") == doc_id:
            return d
    return None


def get_knowledge_summary() -> Dict[str, Any]:
    """Provides high-level stats for the web dashboard."""
    docs = load_documents_metadata()
    active_docs = [d for d in docs if d.get("enabled", True)]
    total_words = sum(d.get("word_count", 0) for d in active_docs)
    total_chars = sum(d.get("char_count", 0) for d in active_docs)
    return {
        "total_docs": len(docs),
        "total_documents": len(docs),
        "active_docs": len(active_docs),
        "active_documents": len(active_docs),
        "total_words": total_words,
        "total_chars": total_chars,
        "has_knowledge": len(active_docs) > 0
    }



def build_knowledge_prompt() -> str:
    """
    Builds the formatted knowledge base prompt section to inject into Gemini Live.
    Only includes active (enabled) documents.
    """
    docs = load_documents_metadata()
    active_docs = [d for d in docs if d.get("enabled", True) and d.get("text", "").strip()]

    if not active_docs:
        return ""

    blocks = [
        "================================================================================",
        "OFFICIAL BUSINESS KNOWLEDGE BASE & REFERENCE MATERIALS:",
        "The business owner has provided the following official documents and reference facts.",
        "When callers ask questions about the business, products, services, pricing, operating",
        "hours, location, return policies, or procedures, YOU MUST BASE YOUR ANSWERS ON THIS",
        "KNOWLEDGE BASE. Answer clearly, accurately, and conversationally in 1-2 natural sentences.",
        "If a caller asks for something not mentioned in this knowledge base, politely inform them",
        "and offer to connect them with a human specialist or take a callback message.",
        "================================================================================"
    ]

    for d in active_docs:
        title = d.get("filename", "Document")
        doc_text = d.get("text", "").strip()
        blocks.append(f"\n--- [OFFICIAL DOCUMENT: {title}] ---\n{doc_text}\n--- [END OF {title}] ---")

    blocks.append("================================================================================")
    return "\n".join(blocks)


LANGUAGE_PROMPTS = {
    "auto": "Spoken Language: Automatically detect the caller's language and respond naturally in the exact same language (supports English, Hindi, Spanish, French, German, and all major languages).",
    "en": "Spoken Language: Speak and respond primarily in English. If the caller speaks in another language, respond naturally in their language.",
    "hi": "Spoken Language: Speak and respond primarily in Hindi (हिंदी). You may mix common English/Hinglish terms naturally as common in modern telephonic conversations in India.",
    "mai": "Spoken Language: Speak and respond primarily in Maithili (मैथिली). Use natural, respectful, and polite conversational Maithili as spoken in Mithila.",
    "es": "Spoken Language: Speak and respond primarily in Spanish (Español). Maintain a polite, natural conversational flow.",
    "fr": "Spoken Language: Speak and respond primarily in French (Français). Maintain a polite, natural conversational flow.",
    "de": "Spoken Language: Speak and respond primarily in German (Deutsch). Maintain a polite, natural conversational flow.",
    "ja": "Spoken Language: Speak and respond primarily in Japanese (日本語). Use polite conversational Japanese (Teineigo).",
    "zh": "Spoken Language: Speak and respond primarily in Mandarin Chinese (中文/普通话). Maintain a polite, natural conversational flow.",
    "ar": "Spoken Language: Speak and respond primarily in Arabic (العربية). Maintain a polite, natural conversational flow.",
    "pt": "Spoken Language: Speak and respond primarily in Portuguese (Português). Maintain a polite, natural conversational flow.",
    "ru": "Spoken Language: Speak and respond primarily in Russian (Русский). Maintain a polite, natural conversational flow.",
    "it": "Spoken Language: Speak and respond primarily in Italian (Italiano). Maintain a polite, natural conversational flow.",
    "ko": "Spoken Language: Speak and respond primarily in Korean (한국어). Maintain a polite, natural conversational flow.",
    "bn": "Spoken Language: Speak and respond primarily in Bengali (বাংলা). Maintain a polite, natural conversational flow.",
    "mr": "Spoken Language: Speak and respond primarily in Marathi (मराठी). Maintain a polite, natural conversational flow.",
    "ta": "Spoken Language: Speak and respond primarily in Tamil (தமிழ்). Maintain a polite, natural conversational flow.",
    "te": "Spoken Language: Speak and respond primarily in Telugu (తెలుగు). Maintain a polite, natural conversational flow.",
    "gu": "Spoken Language: Speak and respond primarily in Gujarati (ગુજરાતી). Maintain a polite, natural conversational flow.",
    "pa": "Spoken Language: Speak and respond primarily in Punjabi (ਪੰਜਾਬੀ). Maintain a polite, natural conversational flow.",
    "kn": "Spoken Language: Speak and respond primarily in Kannada (ಕನ್ನಡ). Maintain a polite, natural conversational flow.",
    "ml": "Spoken Language: Speak and respond primarily in Malayalam (മലയാളം). Maintain a polite, natural conversational flow.",
}


def get_effective_system_instruction(base_instruction: Optional[str] = None, language: Optional[str] = "en") -> str:
    """
    Combines the AI Persona base instruction, language directive, and the active Business Knowledge Base.
    """
    default_base = (
        "You are a helpful, natural, and polite AI phone assistant answering calls for the business. "
        "Speak conversationally in short, crisp sentences (1-2 sentences at a time) as in a real phone call. "
        "Respond immediately and concisely without long preambles."
    )
    instruction = (base_instruction or "").strip() or default_base

    lang_key = (language or "en").strip().lower()
    lang_directive = LANGUAGE_PROMPTS.get(lang_key, LANGUAGE_PROMPTS.get("en", ""))
    if lang_directive:
        instruction = f"{instruction}\n\n[PRIMARY LANGUAGE DIRECTIVE]\n{lang_directive}"

    knowledge = build_knowledge_prompt()

    if knowledge:
        return f"{instruction}\n\n{knowledge}"
    return instruction

