from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, BooleanVar, StringVar, Tk, Toplevel, messagebox, scrolledtext, ttk
import tkinter as tk

import requests
from playwright.async_api import async_playwright

from codex_service import CLIENT_ID, DEFAULT_MODEL, CodexAccount, CodexService, decode_jwt
from print_lens import (
    ClipboardLens,
    GoogleTrafficBlock,
    format_output,
    get_clipboard_image,
    image_hash,
    save_clipboard_image,
)
from voice_service import TTSManager, VoiceService


APP_CONFIG = Path("app_config.json")
DEFAULT_CONFIG = {
    "model": DEFAULT_MODEL,
    "selected_account_id": "",
    "response_depth": "media",
    "reasoning_effort": "medium",
    "tts_enabled": False,
    "tts_provider": "edge",
    "tts_voice": "pt-BR-FranciscaNeural",
    "auto_print": False,
    "lens_enabled": True,
    "send_image_to_llm": True,
}

APP_BG = "#eef2f7"
PANEL_BG = "#ffffff"
PANEL_ALT = "#f8fafc"
TEXT_FG = "#172033"
MUTED_FG = "#667085"
ACCENT = "#4f7cff"
ACCENT_DARK = "#365bd6"
PITICA_GREEN = "#247a4d"

RESPONSE_DEPTH_LABELS = {
    "minima": "mínima",
    "media": "média",
    "maxima": "máxima",
}

RESPONSE_DEPTH_INSTRUCTIONS = {
    "minima": "Modo de resposta minima: responda em 1 a 3 frases curtas, sem lista salvo se for essencial.",
    "media": "Modo de resposta media: responda com clareza e contexto suficiente, sem enrolar.",
    "maxima": "Modo de resposta maxima: explique em detalhes, organize por partes e inclua passos praticos quando fizer sentido.",
}


def clean_markdown_inline(text: str, *, keep_urls: bool) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    if keep_urls:
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    else:
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"(\*\*\*|___)(.*?)\1", r"\2", text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(?<!\w)(\*|_)([^*_]+)\1(?!\w)", r"\2", text)
    return text.replace("\\", "")


def clean_markdown_for_tts(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    text = text.replace("|", " ")
    text = clean_markdown_inline(text, keep_urls=False)
    text = re.sub(r"[*_#`~>\[\](){}]", " ", text)
    return " ".join(text.split())


class PiticaUnifiedApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("Pitica Unified - Print STT Codex")
        self.root.geometry("1220x780")
        self.root.minsize(980, 620)

        self.config = self.load_config()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.pending_prompts: queue.Queue[tuple[str, str, bool, list[str] | None, list[str] | None]] = queue.Queue()
        self.messages: list[dict[str, str]] = []
        self.llm_busy = False
        self.last_print_hash = ""
        self.print_monitor_stop = threading.Event()
        self.print_monitor_thread: threading.Thread | None = None
        self.voice_service: VoiceService | None = None

        self.codex = CodexService(
            root=".",
            model=self.config.get("model", DEFAULT_MODEL),
            selected_account_id=self.config.get("selected_account_id") or None,
        )
        self.tts = TTSManager(
            enabled=bool(self.config.get("tts_enabled", False)),
            provider=str(self.config.get("tts_provider", "edge")),
            voice=str(self.config.get("tts_voice", "pt-BR-FranciscaNeural")),
            on_status=self.log,
        )

        self.model_var = StringVar(value=str(self.config.get("model", DEFAULT_MODEL)))
        self.response_depth_var = StringVar(value=str(self.config.get("response_depth", "media")))
        self.reasoning_effort_var = StringVar(value=str(self.config.get("reasoning_effort", "medium")))
        self.tts_enabled_var = BooleanVar(value=bool(self.config.get("tts_enabled", False)))
        self.tts_provider_var = StringVar(value=str(self.config.get("tts_provider", "edge")))
        self.tts_voice_var = StringVar(value=str(self.config.get("tts_voice", "pt-BR-FranciscaNeural")))
        self.auto_print_var = BooleanVar(value=bool(self.config.get("auto_print", False)))
        self.lens_enabled_var = BooleanVar(value=bool(self.config.get("lens_enabled", True)))
        self.send_image_to_llm_var = BooleanVar(value=bool(self.config.get("send_image_to_llm", True)))

        self._build_ui()
        self.refresh_accounts()
        self.root.after(100, self._drain_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    @staticmethod
    def load_config() -> dict[str, object]:
        if not APP_CONFIG.exists():
            return dict(DEFAULT_CONFIG)
        try:
            data = json.loads(APP_CONFIG.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            return merged
        except Exception:
            return dict(DEFAULT_CONFIG)

    def save_config(self) -> None:
        self.config.update(
            {
                "model": self.model_var.get().strip() or DEFAULT_MODEL,
                "selected_account_id": self.codex.selected_account_id or "",
                "response_depth": self.response_depth_var.get().strip() or "media",
                "reasoning_effort": self.reasoning_effort_var.get().strip() or "medium",
                "tts_enabled": bool(self.tts_enabled_var.get()),
                "tts_provider": self.tts_provider_var.get().strip() or "edge",
                "tts_voice": self.tts_voice_var.get().strip() or "pt-BR-FranciscaNeural",
                "auto_print": bool(self.auto_print_var.get()),
                "lens_enabled": bool(self.lens_enabled_var.get()),
                "send_image_to_llm": bool(self.send_image_to_llm_var.get()),
            }
        )
        APP_CONFIG.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")
        self.log("Config salva.")

    def _build_ui(self) -> None:
        self.root.configure(bg=APP_BG)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", font=("Segoe UI", 10), background=APP_BG, foreground=TEXT_FG)
        style.configure("TFrame", background=APP_BG)
        style.configure("Panel.TFrame", background=PANEL_BG, relief="flat")
        style.configure("Toolbar.TFrame", background=PANEL_BG)
        style.configure("TLabel", background=APP_BG, foreground=TEXT_FG)
        style.configure("Toolbar.TLabel", background=PANEL_BG, foreground=MUTED_FG)
        style.configure("TButton", padding=(12, 7), borderwidth=0, relief="flat", background="#dde5f3", foreground=TEXT_FG)
        style.map("TButton", background=[("active", "#cfdaf0"), ("pressed", "#bdcae6")])
        style.configure("Accent.TButton", padding=(14, 8), borderwidth=0, relief="flat", background=ACCENT, foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("pressed", "#284bb8")])
        style.configure("TCheckbutton", background=APP_BG, foreground=TEXT_FG)
        style.configure("Toolbar.TCheckbutton", background=PANEL_BG, foreground=TEXT_FG)
        style.configure("TEntry", padding=(7, 6), fieldbackground=PANEL_BG)
        style.configure("TCombobox", padding=(6, 5), fieldbackground=PANEL_BG)
        style.configure("TNotebook", background=APP_BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 8), background="#dfe6f2", foreground=TEXT_FG, borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", PANEL_BG), ("active", "#edf2fa")])
        style.configure("Treeview", background=PANEL_BG, fieldbackground=PANEL_BG, foreground=TEXT_FG, rowheight=28, borderwidth=0)
        style.configure("Treeview.Heading", background="#e7edf7", foreground=TEXT_FG, padding=(8, 6), font=("Segoe UI", 10, "bold"))
        style.configure("TLabelframe", background=APP_BG, borderwidth=1, relief="solid")
        style.configure("TLabelframe.Label", background=APP_BG, foreground=MUTED_FG, font=("Segoe UI", 10, "bold"))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=BOTH, expand=True, padx=12, pady=12)

        self._build_chat_tab()
        self._build_print_tab()
        self._build_voice_tab()
        self._build_accounts_tab()
        self._build_config_tab()

    def _build_chat_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Chat")

        toolbar = ttk.Frame(frame, style="Toolbar.TFrame", padding=10)
        toolbar.pack(fill=X, pady=(0, 10))
        ttk.Button(toolbar, text="Novo chat", command=self.new_chat).pack(side=LEFT)
        ttk.Checkbutton(toolbar, text="Falar respostas", variable=self.tts_enabled_var, command=self.apply_tts_config, style="Toolbar.TCheckbutton").pack(side=LEFT, padx=12)
        ttk.Label(toolbar, text="Resposta", style="Toolbar.TLabel").pack(side=LEFT, padx=(10, 4))
        ttk.Combobox(
            toolbar,
            textvariable=self.response_depth_var,
            values=["minima", "media", "maxima"],
            width=9,
            state="readonly",
        ).pack(side=LEFT)
        ttk.Label(toolbar, text="Think", style="Toolbar.TLabel").pack(side=LEFT, padx=(12, 4))
        ttk.Combobox(
            toolbar,
            textvariable=self.reasoning_effort_var,
            values=["none", "low", "medium", "high", "xhigh"],
            width=9,
            state="readonly",
        ).pack(side=LEFT)
        self.llm_status = StringVar(value="Pronto")
        ttk.Label(toolbar, textvariable=self.llm_status, style="Toolbar.TLabel").pack(side=RIGHT)

        self.chat_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=28)
        self.chat_text.pack(fill=BOTH, expand=True)
        self.chat_text.configure(
            state=tk.DISABLED,
            font=("Segoe UI", 10),
            background=PANEL_BG,
            foreground=TEXT_FG,
            relief=tk.FLAT,
            borderwidth=0,
            padx=14,
            pady=12,
            insertbackground=TEXT_FG,
        )
        self.chat_text.tag_configure("role_user", font=("Segoe UI", 10, "bold"), foreground="#0b5394", spacing1=6)
        self.chat_text.tag_configure("role_assistant", font=("Segoe UI", 10, "bold"), foreground=PITICA_GREEN, spacing1=6)
        self.chat_text.tag_configure("md_h1", font=("Segoe UI", 15, "bold"), spacing1=10, spacing3=4)
        self.chat_text.tag_configure("md_h2", font=("Segoe UI", 13, "bold"), spacing1=8, spacing3=3)
        self.chat_text.tag_configure("md_h3", font=("Segoe UI", 11, "bold"), spacing1=6, spacing3=2)
        self.chat_text.tag_configure("md_bold", font=("Segoe UI", 10, "bold"))
        self.chat_text.tag_configure("md_code", font=("Consolas", 10), background="#eeeeee")

        input_bar = ttk.Frame(frame)
        input_bar.pack(fill=X, pady=(10, 0))
        self.prompt_entry = ttk.Entry(input_bar)
        self.prompt_entry.pack(side=LEFT, fill=X, expand=True)
        self.prompt_entry.bind("<Return>", lambda _event: self.send_prompt_from_entry())
        ttk.Button(input_bar, text="Enviar", command=self.send_prompt_from_entry, style="Accent.TButton").pack(side=RIGHT, padx=(8, 0))

    def _build_print_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Print/Lens")

        toolbar = ttk.Frame(frame, style="Toolbar.TFrame", padding=10)
        toolbar.pack(fill=X, pady=(0, 10))
        ttk.Button(toolbar, text="Analisar clipboard", command=self.analyze_clipboard_once, style="Accent.TButton").pack(side=LEFT)
        ttk.Button(toolbar, text="Iniciar monitor", command=self.start_print_monitor).pack(side=LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="Parar monitor", command=self.stop_print_monitor).pack(side=LEFT, padx=(8, 0))
        ttk.Checkbutton(toolbar, text="Usar Lens", variable=self.lens_enabled_var, style="Toolbar.TCheckbutton").pack(side=LEFT, padx=(12, 0))
        ttk.Checkbutton(toolbar, text="Enviar imagem ao LLM", variable=self.send_image_to_llm_var, style="Toolbar.TCheckbutton").pack(side=LEFT, padx=(12, 0))
        ttk.Checkbutton(toolbar, text="Auto print ao iniciar app", variable=self.auto_print_var, style="Toolbar.TCheckbutton").pack(side=LEFT, padx=12)
        self.print_status = StringVar(value="Aguardando print.")
        ttk.Label(toolbar, textvariable=self.print_status, style="Toolbar.TLabel").pack(side=RIGHT)

        self.lens_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=30)
        self.lens_text.pack(fill=BOTH, expand=True)
        self.lens_text.configure(background=PANEL_BG, foreground=TEXT_FG, relief=tk.FLAT, borderwidth=0, padx=12, pady=10)

    def _build_voice_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Voz")

        controls = ttk.Frame(frame, style="Toolbar.TFrame", padding=10)
        controls.pack(fill=X, pady=(0, 10))
        ttk.Button(controls, text="Iniciar STT", command=self.start_voice, style="Accent.TButton").pack(side=LEFT)
        ttk.Button(controls, text="Parar STT", command=self.stop_voice).pack(side=LEFT, padx=(8, 0))
        self.voice_status = StringVar(value="STT parado.")
        ttk.Label(controls, textvariable=self.voice_status, style="Toolbar.TLabel").pack(side=LEFT, padx=14)

        self.level = ttk.Progressbar(frame, orient=tk.HORIZONTAL, mode="determinate", maximum=1000)
        self.level.pack(fill=X, pady=(0, 12))

        tts_box = ttk.LabelFrame(frame, text="TTS opcional", padding=10)
        tts_box.pack(fill=X, pady=(0, 8))
        ttk.Checkbutton(tts_box, text="Falar respostas do LLM", variable=self.tts_enabled_var, command=self.apply_tts_config).pack(side=LEFT)
        ttk.Label(tts_box, text="Provider").pack(side=LEFT, padx=(16, 4))
        ttk.Combobox(tts_box, textvariable=self.tts_provider_var, values=["edge", "off"], width=10, state="readonly").pack(side=LEFT)
        ttk.Label(tts_box, text="Voz").pack(side=LEFT, padx=(16, 4))
        ttk.Entry(tts_box, textvariable=self.tts_voice_var, width=30).pack(side=LEFT)
        ttk.Button(tts_box, text="Testar", command=lambda: self.tts.speak_async("Teste de voz da Pitica.")).pack(side=LEFT, padx=(8, 0))

        self.transcript_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=24)
        self.transcript_text.pack(fill=BOTH, expand=True)
        self.transcript_text.configure(background=PANEL_BG, foreground=TEXT_FG, relief=tk.FLAT, borderwidth=0, padx=12, pady=10)

    def _build_accounts_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Contas")

        toolbar = ttk.Frame(frame, style="Toolbar.TFrame", padding=10)
        toolbar.pack(fill=X, pady=(0, 10))
        ttk.Button(toolbar, text="Recarregar", command=self.refresh_accounts).pack(side=LEFT)
        ttk.Button(toolbar, text="Verificar quotas", command=self.verify_accounts, style="Accent.TButton").pack(side=LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="Usar selecionada", command=self.use_selected_account).pack(side=LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="Injetar conta", command=self.open_device_login).pack(side=LEFT, padx=(8, 0))
        self.account_status = StringVar(value="")
        ttk.Label(toolbar, textvariable=self.account_status, style="Toolbar.TLabel").pack(side=RIGHT)

        columns = ("label", "result", "five", "weekly", "workspace", "email", "file")
        self.accounts_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "label": "Conta",
            "result": "Status",
            "five": "5h",
            "weekly": "Semanal",
            "workspace": "Workspace",
            "email": "Email",
            "file": "Arquivo",
        }
        for key, title in headings.items():
            self.accounts_tree.heading(key, text=title)
        self.accounts_tree.column("label", width=180)
        self.accounts_tree.column("result", width=120)
        self.accounts_tree.column("five", width=80, anchor=tk.CENTER)
        self.accounts_tree.column("weekly", width=80, anchor=tk.CENTER)
        self.accounts_tree.column("workspace", width=160)
        self.accounts_tree.column("email", width=230)
        self.accounts_tree.column("file", width=160)
        self.accounts_tree.pack(fill=BOTH, expand=True)

    def _build_config_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="Config/Logs")

        row = ttk.Frame(frame, style="Toolbar.TFrame", padding=10)
        row.pack(fill=X, pady=(0, 10))
        ttk.Label(row, text="Modelo Codex", style="Toolbar.TLabel").pack(side=LEFT)
        ttk.Entry(row, textvariable=self.model_var, width=28).pack(side=LEFT, padx=(8, 16))
        ttk.Label(row, text="Resposta", style="Toolbar.TLabel").pack(side=LEFT, padx=(0, 4))
        ttk.Combobox(row, textvariable=self.response_depth_var, values=["minima", "media", "maxima"], width=9, state="readonly").pack(side=LEFT, padx=(0, 16))
        ttk.Label(row, text="Think", style="Toolbar.TLabel").pack(side=LEFT, padx=(0, 4))
        ttk.Combobox(row, textvariable=self.reasoning_effort_var, values=["none", "low", "medium", "high", "xhigh"], width=9, state="readonly").pack(side=LEFT, padx=(0, 16))
        ttk.Button(row, text="Salvar config", command=self.save_config, style="Accent.TButton").pack(side=LEFT)

        self.log_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=32)
        self.log_text.pack(fill=BOTH, expand=True)
        self.log_text.configure(background=PANEL_BG, foreground=TEXT_FG, relief=tk.FLAT, borderwidth=0, padx=12, pady=10)
        self.log("App iniciado.")

    def post(self, kind: str, payload: object) -> None:
        self.ui_queue.put((kind, payload))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "chat_delta":
                    self._append_chat(str(payload), raw=True)
                elif kind == "chat_done":
                    self._on_chat_done(str(payload))
                elif kind == "chat_error":
                    self._on_chat_error(str(payload))
                elif kind == "lens_result":
                    self._on_lens_result(payload)  # type: ignore[arg-type]
                elif kind == "print_image":
                    self._handle_print_image(Path(str(payload)))
                elif kind == "voice_status":
                    self.voice_status.set(str(payload))
                    self._append_transcript(f"[status] {payload}\n")
                elif kind == "voice_level":
                    self.level["value"] = min(1000, int(payload))
                elif kind == "voice_transcript":
                    self._on_voice_transcript(str(payload))
                elif kind == "accounts":
                    self._render_account_statuses(payload)  # type: ignore[arg-type]
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    def log(self, message: str) -> None:
        self.post("log", message)

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(END, f"[{stamp}] {message}\n")
        self.log_text.see(END)

    def _append_chat(self, text: str, *, raw: bool = False) -> None:
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.insert(END, text if raw else text + "\n")
        self.chat_text.see(END)
        self.chat_text.configure(state=tk.DISABLED)

    def _append_role(self, role: str, source: str | None = None) -> None:
        label = "Voce" if role == "user" else "Pitica"
        if source:
            label += f" [{source}]"
        tag = "role_user" if role == "user" else "role_assistant"
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.insert(END, f"\n{label}: ", tag)
        self.chat_text.configure(state=tk.DISABLED)

    def _append_markdown(self, text: str) -> None:
        self.chat_text.configure(state=tk.NORMAL)
        in_code = False
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                self.chat_text.insert(END, clean_markdown_inline(line, keep_urls=True) + "\n", "md_code")
                continue

            heading = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", line)
            if heading:
                level = min(3, len(heading.group(1)))
                self._insert_inline_markdown(heading.group(2).strip(), (f"md_h{level}",))
                self.chat_text.insert(END, "\n")
                continue

            bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
            if bullet:
                indent = "  " * max(0, len(bullet.group(1)) // 2)
                self.chat_text.insert(END, f"{indent}- ")
                self._insert_inline_markdown(bullet.group(2), ())
                self.chat_text.insert(END, "\n")
                continue

            self._insert_inline_markdown(line, ())
            self.chat_text.insert(END, "\n")
        self.chat_text.see(END)
        self.chat_text.configure(state=tk.DISABLED)

    def _insert_inline_markdown(self, text: str, base_tags: tuple[str, ...]) -> None:
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
        pattern = re.compile(r"(`[^`]+`)|(\*\*\*.*?\*\*\*|___.*?___|\*\*.*?\*\*|__.*?__)")
        pos = 0
        for match in pattern.finditer(text):
            if match.start() > pos:
                self.chat_text.insert(END, clean_markdown_inline(text[pos:match.start()], keep_urls=True), base_tags)
            token = match.group(0)
            if token.startswith("`"):
                self.chat_text.insert(END, token.strip("`"), base_tags + ("md_code",))
            else:
                self.chat_text.insert(END, clean_markdown_inline(token, keep_urls=True), base_tags + ("md_bold",))
            pos = match.end()
        if pos < len(text):
            self.chat_text.insert(END, clean_markdown_inline(text[pos:], keep_urls=True), base_tags)

    def _append_transcript(self, text: str) -> None:
        self.transcript_text.insert(END, text)
        self.transcript_text.see(END)

    def new_chat(self) -> None:
        self.messages = []
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.delete("1.0", END)
        self.chat_text.configure(state=tk.DISABLED)
        self.llm_status.set("Novo chat.")

    def send_prompt_from_entry(self) -> None:
        text = self.prompt_entry.get().strip()
        if not text:
            return
        self.prompt_entry.delete(0, END)
        self.send_user_message(text, source="texto")

    def send_user_message(
        self,
        text: str,
        *,
        source: str,
        new_chat: bool = False,
        print_paths: list[str] | None = None,
        image_paths: list[str] | None = None,
    ) -> None:
        text = " ".join(text.split()) if source == "voz" else text.strip()
        if not text:
            return
        if self.llm_busy:
            self.pending_prompts.put((text, source, new_chat, print_paths, image_paths))
            self.log(f"LLM ocupado; entrada de {source} entrou na fila.")
            return
        if new_chat:
            self.new_chat()

        self.llm_busy = True
        self.llm_status.set(f"Codex pensando ({source})...")
        self._append_role("user", source)
        self._append_markdown(text)
        self._append_role("assistant")
        self.messages.append({"role": "user", "content": text})
        history = [{"role": "system", "content": self.system_prompt()}] + list(self.messages)
        model = self.model_var.get().strip() or DEFAULT_MODEL
        self.codex.model = model
        reasoning_effort = self.reasoning_effort_var.get().strip() or "medium"

        thread = threading.Thread(
            target=self._llm_worker,
            args=(history, model, print_paths, image_paths, reasoning_effort),
            daemon=True,
        )
        thread.start()

    def _llm_worker(
        self,
        history: list[dict[str, str]],
        model: str,
        print_paths: list[str] | None,
        image_paths: list[str] | None,
        reasoning_effort: str,
    ) -> None:
        try:
            response_text = self.codex.chat(
                history,
                model=model,
                print_paths=print_paths,
                image_paths=image_paths,
                reasoning_effort=reasoning_effort,
            )
            self.post("chat_done", response_text)
        except Exception as exc:
            self.post("chat_error", str(exc))

    def _on_chat_done(self, response_text: str) -> None:
        self.messages.append({"role": "assistant", "content": response_text})
        self._append_markdown(response_text)
        self._append_chat("\n", raw=True)
        self.llm_busy = False
        self.llm_status.set("Pronto")
        self.apply_tts_config()
        self.tts.speak_async(clean_markdown_for_tts(response_text))
        self._run_next_pending_prompt()

    def _on_chat_error(self, error: str) -> None:
        self._append_chat(f"\n[ERRO LLM] {error}\n", raw=True)
        self.log(f"Erro LLM: {error}")
        self.llm_busy = False
        self.llm_status.set("Erro")
        self._run_next_pending_prompt()

    def _run_next_pending_prompt(self) -> None:
        try:
            text, source, new_chat, print_paths, image_paths = self.pending_prompts.get_nowait()
        except queue.Empty:
            return
        self.root.after(
            50,
            lambda: self.send_user_message(
                text,
                source=source,
                new_chat=new_chat,
                print_paths=print_paths,
                image_paths=image_paths,
            ),
        )

    def system_prompt(self) -> str:
        depth = self.response_depth_var.get().strip() or "media"
        depth_instruction = RESPONSE_DEPTH_INSTRUCTIONS.get(depth, RESPONSE_DEPTH_INSTRUCTIONS["media"])
        return (
            "Voce e a Pitica, uma assistente local em pt-BR ligada a print, STT, Google Lens e Codex. "
            "Quando uma imagem for anexada, analise a imagem diretamente. "
            "Se tambem houver dados do Lens, use o RESULTADO GOOGLE LENS como apoio; referencias e URLs sao apenas sinais fracos e podem estar errados. "
            "Evite markdown pesado e excesso de asteriscos. "
            f"{depth_instruction}"
        )

    def analyze_clipboard_once(self) -> None:
        image = get_clipboard_image()
        if image is None:
            self.print_status.set("Nenhuma imagem no clipboard.")
            return
        image_path = save_clipboard_image(image, max_width=1280, jpg_quality=88)
        self._handle_print_image(image_path)

    def start_print_monitor(self) -> None:
        if self.print_monitor_thread and self.print_monitor_thread.is_alive():
            return
        self.print_monitor_stop.clear()
        current = get_clipboard_image()
        self.last_print_hash = image_hash(current) if current else ""
        self.print_monitor_thread = threading.Thread(target=self._print_monitor_loop, daemon=True)
        self.print_monitor_thread.start()
        self.print_status.set("Monitor de print ligado.")
        self.log("Monitor de print ligado.")

    def stop_print_monitor(self) -> None:
        self.print_monitor_stop.set()
        self.print_status.set("Monitor de print parado.")
        self.log("Monitor de print parado.")

    def _print_monitor_loop(self) -> None:
        while not self.print_monitor_stop.is_set():
            try:
                image = get_clipboard_image()
                if image is not None:
                    current_hash = image_hash(image)
                    if current_hash and current_hash != self.last_print_hash:
                        self.last_print_hash = current_hash
                        image_path = save_clipboard_image(image, max_width=1280, jpg_quality=88)
                        self.post("log", f"Print detectado: {image_path}")
                        self.post("print_image", str(image_path))
                time.sleep(0.6)
            except Exception as exc:
                self.post("log", f"Erro no monitor de print: {exc}")
                time.sleep(1.0)

    def _handle_print_image(self, image_path: Path) -> None:
        if self.lens_enabled_var.get():
            self.print_status.set(f"Lens analisando {image_path.name}...")
            self._start_lens_analysis(image_path)
            return
        self._handle_print_without_lens(image_path)

    def _handle_print_without_lens(self, image_path: Path) -> None:
        message = (
            "Lens desativado.\n\n"
            f"Print salvo em:\n{image_path}\n\n"
            "A imagem sera enviada diretamente ao Codex se o botao 'Enviar imagem ao LLM' estiver ativo."
        )
        self.post("log", f"Print salvo com Lens desativado: {image_path}")
        self.print_status.set("Print salvo. Lens desativado.")
        self.lens_text.delete("1.0", END)
        self.lens_text.insert(END, message)
        if self.send_image_to_llm_var.get():
            prompt = (
                "Novo print recebido. O Google Lens esta desativado. "
                "Analise diretamente a imagem anexada e diga o que esta acontecendo nela."
            )
            self.send_user_message(
                prompt,
                source="print/imagem",
                new_chat=True,
                print_paths=[str(image_path)],
                image_paths=[str(image_path)],
            )

    def _start_lens_analysis(self, image_path: Path) -> None:
        thread = threading.Thread(target=self._lens_worker, args=(image_path,), daemon=True)
        thread.start()

    def _lens_worker(self, image_path: Path) -> None:
        async def run() -> dict[str, object]:
            playwright = await async_playwright().start()
            lens = ClipboardLens(playwright=playwright, show=False, upload_timeout=8.0, render_wait=5.0, max_links=4)
            try:
                await lens.start(visible=False)
                answer, refs, timings, url, status, completed = await lens.analyze(image_path)
                return {
                    "image_path": str(image_path),
                    "answer": answer,
                    "refs": refs,
                    "timings": timings,
                    "url": url,
                    "status": status,
                    "completed": completed,
                }
            finally:
                try:
                    await lens.stop()
                    await playwright.stop()
                except Exception:
                    pass

        try:
            result = asyncio.run(run())
            self.post("lens_result", result)
        except GoogleTrafficBlock as block:
            self.post("log", f"Google Lens bloqueou por verificacao manual: {block.url}")
            self.post("chat_error", "Google Lens pediu verificacao manual. Rode o Lens visivel ou tente novamente depois.")
        except Exception as exc:
            self.post("log", f"Erro Lens: {exc}")
            self.post("chat_error", f"Erro Lens: {exc}")

    def _on_lens_result(self, result: dict[str, object]) -> None:
        image_path = str(result.get("image_path", ""))
        answer = str(result.get("answer", ""))
        refs = result.get("refs") or []
        timings = result.get("timings") or {"upload": 0, "render": 0, "total": 0}
        url = str(result.get("url", ""))
        status = int(result.get("status") or 0)
        completed = bool(result.get("completed"))
        formatted = format_output(Path(image_path), answer, refs, timings, url, status, completed)  # type: ignore[arg-type]
        self.lens_text.delete("1.0", END)
        self.lens_text.insert(END, formatted)
        self.print_status.set("Lens concluido.")
        self.log(f"Lens concluido para {Path(image_path).name}.")

        prompt = self._build_print_prompt(image_path, answer, refs, url)
        image_paths = [image_path] if self.send_image_to_llm_var.get() else None
        self.send_user_message(
            prompt,
            source="print/imagem+lens" if image_paths else "print/lens",
            new_chat=True,
            print_paths=[image_path],
            image_paths=image_paths,
        )

    @staticmethod
    def _build_print_prompt(image_path: str, answer: str, refs: object, url: str) -> str:
        refs_text = ""
        if isinstance(refs, list):
            lines = []
            for index, item in enumerate(refs, 1):
                try:
                    title, href = item
                except Exception:
                    title, href = str(item), ""
                lines.append(f"{index}. {title}\n{href}")
            refs_text = "\n".join(lines)
        return (
            "Novo print recebido. A imagem real esta anexada quando o modo de envio de imagem estiver ativo. "
            "O app tambem enviou a imagem ao Google Lens e este foi o contexto extraido.\n\n"
            f"Arquivo local do print: {image_path}\n\n"
            "FONTE PRINCIPAL - RESULTADO GOOGLE LENS:\n"
            f"{answer}\n\n"
            "FONTE SECUNDARIA - REFERENCIAS DE BAIXA PRIORIDADE:\n"
            "As referencias abaixo sao apenas palpites/resultados relacionados do Lens. "
            "Use somente se baterem claramente com o resultado principal; ignore se parecerem aleatorias.\n"
            f"{refs_text or '-'}\n\n"
            "Analise o print priorizando o RESULTADO GOOGLE LENS. "
            "Nao baseie a resposta em referencias soltas, URL do Google ou tempos tecnicos."
        )

    def start_voice(self) -> None:
        if self.voice_service and self.voice_service.running:
            return
        self.apply_tts_config()
        self.voice_service = VoiceService(
            pause_event=self.tts.speaking_event,
            on_transcript=lambda text: self.post("voice_transcript", text),
            on_status=lambda message: self.post("voice_status", message),
            on_level=lambda level: self.post("voice_level", level),
        )
        try:
            self.voice_service.start()
        except Exception as exc:
            messagebox.showerror("STT", str(exc))
            self.log(f"Erro ao iniciar STT: {exc}")

    def stop_voice(self) -> None:
        if self.voice_service:
            self.voice_service.stop()

    def _on_voice_transcript(self, text: str) -> None:
        self._append_transcript(f"Voce: {text}\n")
        self.send_user_message(text, source="voz")

    def apply_tts_config(self) -> None:
        self.tts.configure(
            enabled=bool(self.tts_enabled_var.get()),
            provider=self.tts_provider_var.get().strip() or "edge",
            voice=self.tts_voice_var.get().strip() or "pt-BR-FranciscaNeural",
        )

    def refresh_accounts(self) -> None:
        accounts = self.codex.list_accounts(refresh_cache=True)
        rows = []
        for account in accounts:
            rows.append(
                {
                    "id": account.id,
                    "label": account.label,
                    "file": account.auth_file.name,
                    "email": account.email,
                    "result": "selecionada" if account.id == self.codex.selected_account_id else "nao verificada",
                    "quota": {},
                    "workspace": "-",
                }
            )
        self._render_account_statuses(rows)
        self.account_status.set(f"{len(rows)} conta(s).")

    def verify_accounts(self) -> None:
        accounts = self.codex.list_accounts(refresh_cache=True)
        self.account_status.set("Verificando...")
        threading.Thread(target=self._verify_accounts_worker, args=(accounts,), daemon=True).start()

    def _verify_accounts_worker(self, accounts: list[CodexAccount]) -> None:
        rows = []
        for account in accounts:
            rows.append(self.codex.verify_account(account))
            self.post("accounts", list(rows))
        self.post("log", f"Verificacao de contas concluida: {len(rows)} conta(s).")

    def _render_account_statuses(self, rows: list[dict[str, object]]) -> None:
        self.accounts_tree.delete(*self.accounts_tree.get_children())
        for row in rows:
            quota = row.get("quota") if isinstance(row.get("quota"), dict) else {}
            five = "-"
            weekly = "-"
            if isinstance(quota, dict):
                if quota.get("five_hour_pct") is not None:
                    five = f"{int(float(quota['five_hour_pct']))}%"
                if quota.get("weekly_pct") is not None:
                    weekly = f"{int(float(quota['weekly_pct']))}%"
            values = (
                row.get("label", "-"),
                row.get("result", "-"),
                five,
                weekly,
                row.get("workspace", "-"),
                row.get("email", "-"),
                row.get("file", "-"),
            )
            self.accounts_tree.insert("", END, iid=str(row.get("id", "")), values=values)
        self.account_status.set(f"{len(rows)} conta(s).")

    def use_selected_account(self) -> None:
        selected = self.accounts_tree.selection()
        if not selected:
            messagebox.showinfo("Contas", "Selecione uma conta primeiro.")
            return
        account_id = selected[0]
        self.codex.select_account(account_id)
        self.save_config()
        self.refresh_accounts()
        self.log(f"Conta selecionada: {account_id}")

    def open_device_login(self) -> None:
        win = Toplevel(self.root)
        win.title("Injetar conta Codex")
        win.geometry("520x310")
        win.transient(self.root)

        ttk.Label(win, text="Device login OpenAI Codex").pack(pady=(16, 8))
        info = StringVar(value="Gerando codigo...")
        code = StringVar(value="----")
        ttk.Label(win, textvariable=info).pack(pady=4)
        code_label = ttk.Label(win, textvariable=code, font=("Consolas", 24, "bold"))
        code_label.pack(pady=8)
        status = StringVar(value="")
        ttk.Label(win, textvariable=status).pack(pady=4)
        open_button = ttk.Button(win, text="Abrir navegador", state=tk.DISABLED)
        open_button.pack(pady=8)

        def copy_code(_event: object) -> None:
            value = code.get()
            if value and value != "----":
                self.root.clipboard_clear()
                self.root.clipboard_append(value)
                status.set("Codigo copiado.")

        code_label.bind("<Button-1>", copy_code)

        def worker() -> None:
            try:
                response = requests.post(
                    "https://auth.openai.com/api/accounts/deviceauth/usercode",
                    json={"client_id": CLIENT_ID},
                    timeout=15,
                )
                if response.status_code != 200:
                    self.root.after(0, lambda: info.set(f"Erro HTTP {response.status_code} ao gerar codigo."))
                    return
                data = response.json()
                user_code = data.get("user_code")
                device_auth_id = data.get("device_auth_id")
                verify_url = data.get("verification_uri") or "https://auth.openai.com/codex/device"

                self.root.after(0, lambda: code.set(str(user_code)))
                self.root.after(0, lambda: info.set("Codigo pronto. Clique nele para copiar."))
                self.root.after(0, lambda: open_button.configure(state=tk.NORMAL, command=lambda: webbrowser.open(verify_url)))

                for attempt in range(60):
                    self.root.after(0, lambda a=attempt: status.set(f"Aguardando aprovacao... {a + 1}/60"))
                    poll = requests.post(
                        "https://auth.openai.com/api/accounts/deviceauth/token",
                        json={"client_id": CLIENT_ID, "device_auth_id": device_auth_id, "user_code": user_code},
                        timeout=15,
                    )
                    if poll.status_code == 200:
                        poll_data = poll.json()
                        final = requests.post(
                            "https://auth.openai.com/oauth/token",
                            data={
                                "grant_type": "authorization_code",
                                "client_id": CLIENT_ID,
                                "code": poll_data.get("authorization_code"),
                                "code_verifier": poll_data.get("code_verifier"),
                                "redirect_uri": "https://auth.openai.com/deviceauth/callback",
                            },
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                            timeout=30,
                        )
                        if final.status_code != 200:
                            self.root.after(0, lambda: status.set(f"Erro final HTTP {final.status_code}."))
                            return
                        self._save_injected_account(final.json())
                        self.root.after(0, lambda: status.set("Conta injetada."))
                        self.root.after(0, self.refresh_accounts)
                        self.root.after(2000, win.destroy)
                        return
                    time.sleep(5)
                self.root.after(0, lambda: status.set("Tempo esgotado."))
            except Exception as exc:
                error = str(exc)
                self.root.after(0, lambda: status.set(f"Erro: {error}"))

        threading.Thread(target=worker, daemon=True).start()

    def _save_injected_account(self, tokens: dict[str, object]) -> None:
        access = str(tokens.get("access_token") or "")
        payload = decode_jwt(access)
        profile = payload.get("https://api.openai.com/profile", {}) if isinstance(payload, dict) else {}
        email = profile.get("email") or payload.get("email") or ""
        label = str(email).split("@")[0] if "@" in str(email) else f"injetada_{datetime.now().strftime('%H%M')}"
        auth = payload.get("https://api.openai.com/auth", {}) if isinstance(payload, dict) else {}
        account_id = auth.get("chatgpt_account_id") or ""

        target = Path("auth(infinity).json")
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                data = {"credential_pool": {"openai-codex": []}}
        else:
            data = {"credential_pool": {"openai-codex": []}}
        data.setdefault("credential_pool", {})
        pool = data["credential_pool"].setdefault("openai-codex", [])
        pool.append(
            {
                "label": label,
                "auth_type": "oauth",
                "access_token": access,
                "refresh_token": tokens.get("refresh_token"),
                "account_id": account_id,
            }
        )
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.log(f"Conta adicionada em {target.name}: {label}")

    def on_close(self) -> None:
        self.stop_print_monitor()
        self.stop_voice()
        self.save_config()
        self.root.destroy()


def main() -> None:
    root = Tk()
    app = PiticaUnifiedApp(root)
    if app.auto_print_var.get():
        app.start_print_monitor()
    root.mainloop()


if __name__ == "__main__":
    main()
