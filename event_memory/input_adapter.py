"""TXT入口适配到既有Markdown转录格式，保持原文与引用定位不变。"""
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .errors import MemoryError
from .transcript import parse_transcript


@dataclass(frozen=True)
class PreparedInput:
    markdown: str
    filename: str
    input_format: str

    @property
    def metadata(self):
        return {"input_format": self.input_format, "input_filename": self.filename,
                "input_text_sha256": hashlib.sha256(self.markdown.encode("utf-8")).hexdigest(),
                "input_adapter": "txt-to-md-verbatim-1" if self.input_format == "txt" else "md-passthrough-1"}


def txt_to_markdown(text: str) -> str:
    """已有时间/说话人的转录正文也是合法MD；不添加标题或改行，避免原文定位漂移。"""
    if not isinstance(text, str):
        raise MemoryError("invalid_input", "TXT内容必须是文本。")
    text = text.removeprefix("\ufeff")
    parse_transcript(text)
    return text


def prepare_input(text: str, filename: str = "transcript.md") -> PreparedInput:
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        raise MemoryError("invalid_input", "需要有效的TXT或MD文件名。")
    path = Path(filename)
    suffix = path.suffix.lower()
    if suffix not in {".txt", ".md"}:
        raise MemoryError("unsupported_input", "目前仅支持UTF-8的TXT或MD转录文件。")
    if not isinstance(text, str):
        raise MemoryError("invalid_input", "转录正文必须是文本。")
    text = text.removeprefix("\ufeff")
    markdown = txt_to_markdown(text) if suffix == ".txt" else text
    if suffix == ".md":
        parse_transcript(markdown)
    return PreparedInput(markdown, path.name, suffix[1:])


def load_input(path: str | Path) -> PreparedInput:
    path = Path(path)
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeError:
        raise MemoryError("invalid_encoding", "转录文件必须为UTF-8编码，请先转换编码后导入。") from None
    except OSError:
        raise MemoryError("input_read_failed", "无法读取转录文件，请检查路径和文件权限。") from None
    return prepare_input(text, path.name)


def convert_txt_file(path: str | Path, output: str | Path | None = None) -> Path:
    source = Path(path)
    if source.suffix.lower() != ".txt":
        raise MemoryError("unsupported_input", "TXT转MD命令仅接收.txt文件。")
    document = load_input(source)
    target = Path(output) if output is not None else source.with_suffix(".md")
    if target.suffix.lower() != ".md" or target.resolve() == source.resolve():
        raise MemoryError("invalid_output", "转换输出须为独立的.md文件，不能覆盖输入。")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as stream:
            stream.write(document.markdown.encode("utf-8"))
    except FileExistsError:
        raise MemoryError("output_exists", "目标MD文件已存在，未覆盖；请另选输出路径。") from None
    except OSError:
        raise MemoryError("output_write_failed", "无法保存转换后的MD文件，请检查目录权限。") from None
    return target
