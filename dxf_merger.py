"""
DXF Merger v1.1
Unificador de pranchas DXF exportadas do Revit.

Correção estrutural:
- usa ezdxf para recriar handles e vínculos internos válidos;
- importa blocos e recursos usados pelas entidades;
- cria uma aba de layout para cada prancha de entrada;
- ajusta as viewports para os modelos deslocados;
- valida o arquivo final antes de concluir.

Uso:
    python dxf_merger_corrigido.py

Requisitos:
    Python 3.9+
    pip install ezdxf
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import re
import sys
import threading
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
#  CORE — Merge estrutural de DXF com ezdxf
# ═══════════════════════════════════════════════════════════════

class MissingDependencyError(RuntimeError):
    """Biblioteca necessária não instalada."""


def _load_ezdxf():
    """Carrega ezdxf apenas quando o merge é iniciado, preservando a abertura da UI."""
    try:
        import ezdxf
        from ezdxf import bbox, recover, transform
        from ezdxf.addons import Importer
        from ezdxf.lldxf.const import DXFStructureError
        from ezdxf.math import Matrix44, Vec3
    except ImportError as exc:
        raise MissingDependencyError(
            "A biblioteca 'ezdxf' não está instalada.\n\n"
            "Abra o Prompt de Comando e execute:\n"
            "py -m pip install ezdxf"
        ) from exc
    return ezdxf, bbox, recover, transform, Importer, DXFStructureError, Matrix44, Vec3


_INVALID_LAYOUT_CHARS = re.compile(r'[<>/\\":;?*|=]')
_VERSION_ORDER = {
    "AC1009": 0,   # R12
    "AC1012": 1,   # R13
    "AC1014": 2,   # R14
    "AC1015": 3,   # R2000
    "AC1018": 4,   # R2004
    "AC1021": 5,   # R2007
    "AC1024": 6,   # R2010
    "AC1027": 7,   # R2013
    "AC1032": 8,   # R2018+
}


def _sanitize_layout_name(name):
    """Gera um nome de layout aceito pelo AutoCAD."""
    cleaned = _INVALID_LAYOUT_CHARS.sub("_", str(name)).strip().strip(".")
    if not cleaned or cleaned.casefold() == "model":
        cleaned = "Folha"
    return cleaned[:250]


def _unique_layout_name(doc, desired):
    """Retorna nome de layout único, sem diferenciar maiúsculas/minúsculas."""
    desired = _sanitize_layout_name(desired)
    existing = {name.casefold() for name in doc.layouts.names()}
    if desired.casefold() not in existing:
        return desired

    base = desired[:238]
    number = 2
    while True:
        candidate = f"{base} ({number})"
        if candidate.casefold() not in existing:
            return candidate
        number += 1


def _read_dxf(path, ezdxf, recover, DXFStructureError):
    """Lê normalmente e, se necessário, tenta a rotina de recuperação do ezdxf."""
    try:
        return ezdxf.readfile(path), False, None
    except (OSError, DXFStructureError) as direct_error:
        try:
            doc, auditor = recover.readfile(path, errors="surrogateescape")
            return doc, True, auditor
        except Exception as recovery_error:
            raise RuntimeError(
                f"Não foi possível ler o DXF '{os.path.basename(path)}'.\n"
                f"Leitura normal: {direct_error}\n"
                f"Recuperação: {recovery_error}"
            ) from recovery_error


def _model_extents(doc, bbox):
    """Retorna xmin, ymin, xmax, ymax do modelspace, com fallback seguro."""
    try:
        box = bbox.extents(doc.modelspace(), fast=True)
        if box.has_data:
            values = (box.extmin.x, box.extmin.y, box.extmax.x, box.extmax.y)
            if all(float("-inf") < float(v) < float("inf") for v in values):
                return values
    except Exception:
        pass
    return 0.0, 0.0, 1.0, 1.0


def _shift_layout_viewports(layout, ox, oy, Vec3):
    """
    Mantém a mesma vista depois que o conteúdo do modelspace é transladado.

    O centro da viewport no papel não é alterado. Somente os pontos da vista
    no modelspace são deslocados.
    """
    shifted = 0
    for viewport in layout.query("VIEWPORT"):
        try:
            viewport_id = int(viewport.dxf.get("id", 2))
        except (TypeError, ValueError):
            viewport_id = 2

        # ID 1 é a viewport geral do próprio paperspace.
        if viewport_id <= 1:
            continue

        if viewport.dxf.hasattr("view_center_point"):
            center = Vec3(viewport.dxf.view_center_point)
            viewport.dxf.view_center_point = (center.x + ox, center.y + oy)

        if viewport.dxf.hasattr("view_target_point"):
            target = Vec3(viewport.dxf.view_target_point)
            viewport.dxf.view_target_point = target + Vec3(ox, oy, 0.0)

        shifted += 1
    return shifted


def merge_dxf_files(input_files, output_path, progress_callback=None):
    """
    Mescla múltiplos DXFs em um documento estruturalmente válido.

    Estratégia:
      1. cada modelspace é importado por uma biblioteca DXF, que recria handles,
         proprietários, tabelas, blocos e referências;
      2. os modelos são distribuídos horizontalmente, sem sobreposição;
      3. cada paperspace de origem vira uma aba de layout independente;
      4. as viewports são ajustadas pelo mesmo deslocamento do modelspace;
      5. o resultado é auditado, salvo de forma atômica e reaberto para validação.
    """
    (
        ezdxf,
        bbox,
        recover,
        transform,
        Importer,
        DXFStructureError,
        Matrix44,
        Vec3,
    ) = _load_ezdxf()

    def cb(pct, msg):
        if progress_callback:
            progress_callback(int(pct), msg)

    if not input_files:
        raise ValueError("Nenhum arquivo DXF foi informado.")

    normalized_inputs = [os.path.abspath(os.fspath(path)) for path in input_files]
    output_path = os.path.abspath(os.fspath(output_path))

    if output_path.casefold() in {path.casefold() for path in normalized_inputs}:
        raise ValueError("O arquivo de saída não pode substituir um dos arquivos de entrada.")

    output_dir = os.path.dirname(output_path) or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    cb(0, "Verificando arquivos...")
    loaded = []
    recovered_count = 0

    for index, path in enumerate(normalized_inputs, start=1):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Arquivo não encontrado: {path}")
        if os.path.getsize(path) == 0:
            raise ValueError(f"O arquivo '{os.path.basename(path)}' está vazio.")

        pct = 3 + (index - 1) / len(normalized_inputs) * 22
        cb(pct, f"Lendo {os.path.basename(path)}...")
        doc, was_recovered, source_auditor = _read_dxf(
            path, ezdxf, recover, DXFStructureError
        )
        recovered_count += int(was_recovered)
        loaded.append({
            "path": path,
            "name": Path(path).stem,
            "doc": doc,
            "recovered": was_recovered,
            "source_auditor": source_auditor,
            "bbox": _model_extents(doc, bbox),
        })

    # Usar a versão DXF mais nova entre as entradas. Se houver versão não
    # reconhecida, R2018 é a opção segura para o AutoCAD atual.
    output_version = max(
        (item["doc"].dxfversion for item in loaded),
        key=lambda version: _VERSION_ORDER.get(version, _VERSION_ORDER["AC1032"]),
    )
    if output_version not in _VERSION_ORDER:
        output_version = "AC1032"

    cb(28, "Criando o desenho de destino...")
    target = ezdxf.new(dxfversion=output_version, setup=True)

    # Preservar unidades do primeiro arquivo quando definidas.
    try:
        target.units = loaded[0]["doc"].units
    except Exception:
        pass

    # É obrigatório existir ao menos um paperspace. Mantemos um temporário
    # até que a primeira folha seja importada.
    temp_layout_name = "__DXF_MERGER_TEMP__"
    if "Layout1" in target.layouts:
        target.layouts.rename("Layout1", temp_layout_name)

    target_msp = target.modelspace()
    x_cursor = 0.0
    imported_layouts = 0
    imported_model_entities = 0
    shifted_viewports = 0
    transform_warnings = []

    total = len(loaded)
    for index, item in enumerate(loaded, start=1):
        source = item["doc"]
        filename = os.path.basename(item["path"])
        cb(30 + (index - 1) / total * 52, f"Mesclando {filename}...")

        xmin, ymin, xmax, ymax = item["bbox"]
        model_width = max(float(xmax - xmin), 1.0)
        ox = x_cursor - float(xmin)
        oy = 0.0

        importer = Importer(source, target)

        # Importar modelspace e capturar somente as entidades recém-criadas.
        before_count = len(target_msp)
        importer.import_modelspace(target_layout=target_msp)
        imported_now = list(target_msp)[before_count:]
        imported_model_entities += len(imported_now)

        logger = transform.inplace(
            imported_now,
            Matrix44.translate(ox, oy, 0.0),
        )
        if len(logger):
            transform_warnings.extend(
                f"{filename}: {message}" for message in logger.messages()
            )

        source_paperspaces = [
            layout for layout in source.layouts
            if layout.name.casefold() != "model"
        ]

        for source_layout in source_paperspaces:
            imported_layout = importer.import_paperspace_layout(source_layout.name)

            if len(source_paperspaces) == 1:
                desired_name = item["name"]
            else:
                desired_name = f"{item['name']} - {source_layout.name}"

            final_name = _unique_layout_name(target, desired_name)
            old_name = imported_layout.name
            if old_name != final_name:
                target.layouts.rename(old_name, final_name)
            imported_layout = target.layouts.get(final_name)

            shifted_viewports += _shift_layout_viewports(
                imported_layout, ox, oy, Vec3
            )
            imported_layouts += 1

        # Importa recursos dependentes: blocos, layers, linetypes, estilos e
        # dimstyles. Também resolve nomes repetidos entre arquivos diferentes.
        importer.finalize()

        # Gap proporcional, com mínimo de 50 unidades do próprio desenho.
        x_cursor += model_width + max(model_width * 0.05, 50.0)
        cb(30 + index / total * 52, f"Concluído: {filename}")

    if imported_layouts:
        if temp_layout_name in target.layouts:
            target.layouts.delete(temp_layout_name)
    elif temp_layout_name in target.layouts:
        target.layouts.rename(temp_layout_name, "Layout1")

    cb(85, "Auditando a estrutura DXF...")
    auditor = target.audit()
    if auditor.has_errors:
        details = "; ".join(str(error) for error in auditor.errors[:8])
        raise RuntimeError(
            "A auditoria encontrou erros estruturais não corrigidos no DXF final. "
            + details
        )

    # Gravação atômica: o arquivo final só é substituído depois de concluído.
    temp_output = output_path + ".tmp.dxf"
    try:
        cb(91, "Gravando arquivo temporário...")
        target.saveas(temp_output)

        cb(96, "Validando o arquivo gravado...")
        validation_doc = ezdxf.readfile(temp_output)
        validation_auditor = validation_doc.audit()
        if validation_auditor.has_errors:
            details = "; ".join(
                str(error) for error in validation_auditor.errors[:8]
            )
            raise RuntimeError(
                "O arquivo foi gravado, mas falhou na validação de releitura. "
                + details
            )

        os.replace(temp_output, output_path)
    finally:
        if os.path.exists(temp_output):
            try:
                os.remove(temp_output)
            except OSError:
                pass

    cb(100, f"✓ Concluído! {total} arquivo{'s' if total != 1 else ''} mesclado{'s' if total != 1 else ''}.")

    # Informações úteis no console para diagnóstico, sem interromper o usuário.
    print(
        "DXF Merger v1.1:",
        f"arquivos={total},",
        f"entidades_model={imported_model_entities},",
        f"layouts={imported_layouts},",
        f"viewports_ajustadas={shifted_viewports},",
        f"fontes_recuperadas={recovered_count},",
        f"avisos_transformacao={len(transform_warnings)}",
    )
    for warning in transform_warnings[:20]:
        print("AVISO:", warning, file=sys.stderr)

    return True


# ═══════════════════════════════════════════════════════════════
#  UI — Interface gráfica com tkinter
# ═══════════════════════════════════════════════════════════════

COLORS = {
    'bg':           '#F4F3F0',
    'card':         '#FFFFFF',
    'accent':       '#185FA5',
    'accent_dark':  '#0C447C',
    'text':         '#2C2C2A',
    'muted':        '#5F5E5A',
    'border':       '#D3D1C7',
    'border_light': '#E8E6E0',
    'success':      '#3B6D11',
    'error':        '#A32D2D',
    'list_bg':      '#FAFAF8',
    'list_sel':     '#185FA5',
}


class DXFMergerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DXF Merger v1.1 — Unificador de Pranchas Revit")
        self.root.minsize(760, 520)
        self.root.configure(bg=COLORS['bg'])
        self.files = []   # lista de caminhos completos
        self._build_styles()
        self._build_ui()

    # ── Estilos ttk ────────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style()
        try:
            s.theme_use('clam')
        except Exception:
            pass

        s.configure('App.TFrame',   background=COLORS['bg'])
        s.configure('Card.TFrame',  background=COLORS['card'])

        s.configure('App.TLabel',   background=COLORS['bg'],   foreground=COLORS['text'],  font=('Segoe UI', 10))
        s.configure('Muted.TLabel', background=COLORS['bg'],   foreground=COLORS['muted'], font=('Segoe UI', 9))
        s.configure('Card.TLabel',  background=COLORS['card'],  foreground=COLORS['text'],  font=('Segoe UI', 10))
        s.configure('CardSub.TLabel', background=COLORS['card'], foreground=COLORS['muted'], font=('Segoe UI', 9))
        s.configure('Title.TLabel', background=COLORS['bg'],   foreground=COLORS['text'],  font=('Segoe UI', 16, 'bold'))

        s.configure('Primary.TButton',
                    background=COLORS['accent'], foreground='white',
                    font=('Segoe UI', 10, 'bold'), padding=(16, 8), relief='flat')
        s.map('Primary.TButton',
              background=[('active', COLORS['accent_dark']), ('disabled', COLORS['border'])])

        s.configure('Ghost.TButton',
                    background=COLORS['border'], foreground=COLORS['text'],
                    font=('Segoe UI', 10), padding=(10, 6), relief='flat')
        s.map('Ghost.TButton', background=[('active', '#C0BEB5')])

        s.configure('Small.TButton',
                    background=COLORS['border_light'], foreground=COLORS['muted'],
                    font=('Segoe UI', 9), padding=(6, 4), relief='flat')
        s.map('Small.TButton', background=[('active', COLORS['border'])])

        s.configure('Merge.TProgressbar',
                    troughcolor=COLORS['border_light'],
                    background=COLORS['accent'],
                    thickness=8)

    # ── Layout principal ────────────────────────────────────────────────────

    def _build_ui(self):
        C = COLORS
        pad = dict(padx=24, pady=16)

        root = self.root

        # ── Cabeçalho ──────────────────────────────────────────────────────
        header = tk.Frame(root, bg=C['card'], bd=0)
        header.pack(fill='x')
        tk.Label(header, text="DXF Merger",
                 bg=C['card'], fg=C['text'],
                 font=('Segoe UI', 16, 'bold')).pack(side='left', padx=24, pady=16)
        tk.Label(header, text="Unifica pranchas DXF exportadas do Revit",
                 bg=C['card'], fg=C['muted'],
                 font=('Segoe UI', 10)).pack(side='left', pady=16)
        tk.Frame(root, bg=C['border'], height=1).pack(fill='x')

        # ── Corpo ──────────────────────────────────────────────────────────
        body = tk.Frame(root, bg=C['bg'])
        body.pack(fill='both', expand=True, padx=20, pady=16)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=0, minsize=260)
        body.rowconfigure(0, weight=1)

        # ── Coluna esquerda: lista de arquivos ─────────────────────────────
        left = tk.Frame(body, bg=C['card'], bd=1, relief='flat',
                        highlightthickness=1, highlightbackground=C['border'])
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 12))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        # Cabeçalho da coluna
        lhdr = tk.Frame(left, bg=C['card'])
        lhdr.grid(row=0, column=0, sticky='ew', padx=16, pady=(14, 0))
        tk.Label(lhdr, text="Arquivos de entrada",
                 bg=C['card'], fg=C['text'],
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        tk.Label(lhdr, text="Selecione os arquivos .dxf na ordem das pranchas",
                 bg=C['card'], fg=C['muted'],
                 font=('Segoe UI', 9)).pack(anchor='w', pady=(2, 8))
        tk.Frame(lhdr, bg=C['border'], height=1).pack(fill='x')

        # Listbox
        lbframe = tk.Frame(left, bg=C['card'])
        lbframe.grid(row=1, column=0, sticky='nsew', padx=8, pady=8)
        lbframe.rowconfigure(0, weight=1)
        lbframe.columnconfigure(0, weight=1)

        self.listbox = tk.Listbox(
            lbframe,
            selectmode='extended',
            font=('Segoe UI', 9),
            bg=C['list_bg'],
            fg=C['text'],
            selectbackground=C['list_sel'],
            selectforeground='white',
            relief='flat',
            bd=0,
            activestyle='none',
            highlightthickness=0,
        )
        self.listbox.grid(row=0, column=0, sticky='nsew')
        sb = tk.Scrollbar(lbframe, orient='vertical', command=self.listbox.yview,
                          bg=C['border'])
        sb.grid(row=0, column=1, sticky='ns')
        self.listbox.config(yscrollcommand=sb.set)

        # Botões de manipulação da lista
        lbtns = tk.Frame(left, bg=C['card'])
        lbtns.grid(row=2, column=0, sticky='ew', padx=12, pady=(0, 12))

        ttk.Button(lbtns, text="＋  Adicionar arquivos",
                   style='Primary.TButton',
                   command=self._add_files).pack(side='left', padx=(0, 6))
        ttk.Button(lbtns, text="↑",
                   style='Ghost.TButton', width=2,
                   command=self._move_up).pack(side='left', padx=(0, 4))
        ttk.Button(lbtns, text="↓",
                   style='Ghost.TButton', width=2,
                   command=self._move_down).pack(side='left', padx=(0, 4))
        ttk.Button(lbtns, text="Remover",
                   style='Small.TButton',
                   command=self._remove_selected).pack(side='right')

        # ── Coluna direita: configurações + ação ───────────────────────────
        right = tk.Frame(body, bg=C['card'], bd=1, relief='flat',
                         highlightthickness=1, highlightbackground=C['border'])
        right.grid(row=0, column=1, sticky='nsew')
        right.columnconfigure(0, weight=1)

        # Seção: Arquivo de saída
        rsec = tk.Frame(right, bg=C['card'])
        rsec.pack(fill='x', padx=16, pady=(16, 0))

        tk.Label(rsec, text="Configurações",
                 bg=C['card'], fg=C['text'],
                 font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        tk.Frame(rsec, bg=C['border'], height=1).pack(fill='x', pady=(10, 14))

        tk.Label(rsec, text="Arquivo de saída",
                 bg=C['card'], fg=C['muted'],
                 font=('Segoe UI', 9)).pack(anchor='w')

        outrow = tk.Frame(rsec, bg=C['card'])
        outrow.pack(fill='x', pady=(4, 0))
        outrow.columnconfigure(0, weight=1)

        self.output_var = tk.StringVar(value="merged_pranchas.dxf")
        self.out_entry = tk.Entry(
            outrow, textvariable=self.output_var,
            font=('Segoe UI', 9),
            bg='#F9F8F5', fg=C['text'],
            relief='flat', bd=1,
            highlightthickness=1,
            highlightbackground=C['border'],
            highlightcolor=C['accent'],
        )
        self.out_entry.grid(row=0, column=0, sticky='ew', padx=(0, 6), ipady=5)

        ttk.Button(outrow, text="...", style='Ghost.TButton', width=3,
                   command=self._browse_output).grid(row=0, column=1)

        # Sumário de arquivos
        tk.Frame(rsec, bg=C['border'], height=1).pack(fill='x', pady=14)
        tk.Label(rsec, text="Resumo",
                 bg=C['card'], fg=C['muted'],
                 font=('Segoe UI', 9)).pack(anchor='w')
        self.summary_label = tk.Label(rsec, text="Nenhum arquivo selecionado.",
                                      bg=C['card'], fg=C['muted'],
                                      font=('Segoe UI', 9),
                                      justify='left', wraplength=210)
        self.summary_label.pack(anchor='w', pady=(6, 0))

        # Spacer
        tk.Frame(right, bg=C['card']).pack(fill='both', expand=True)

        # Área de ação
        action = tk.Frame(right, bg=C['card'])
        action.pack(fill='x', padx=16, pady=16)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            action, variable=self.progress_var,
            maximum=100, style='Merge.TProgressbar'
        )
        self.progress_bar.pack(fill='x', pady=(0, 6))

        self.progress_label = tk.Label(action, text="",
                                       bg=C['card'], fg=C['muted'],
                                       font=('Segoe UI', 8))
        self.progress_label.pack(anchor='w', pady=(0, 10))

        self.merge_btn = ttk.Button(action, text="⚡  Mesclar arquivos",
                                    style='Primary.TButton',
                                    command=self._start_merge)
        self.merge_btn.pack(fill='x', ipady=4)

        self.status_label = tk.Label(action, text="",
                                     bg=C['card'], fg=C['muted'],
                                     font=('Segoe UI', 9),
                                     wraplength=210, justify='left')
        self.status_label.pack(anchor='w', pady=(10, 0))

    # ── Ações ──────────────────────────────────────────────────────────────

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="Selecionar arquivos DXF",
            filetypes=[("Arquivos DXF", "*.dxf *.DXF"), ("Todos os arquivos", "*.*")]
        )
        added = 0
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.listbox.insert('end', os.path.basename(p))
                added += 1
        if added:
            self._update_summary()

    def _remove_selected(self):
        sel = sorted(self.listbox.curselection(), reverse=True)
        for i in sel:
            self.listbox.delete(i)
            self.files.pop(i)
        self._update_summary()

    def _move_up(self):
        sel = list(self.listbox.curselection())
        if not sel or sel[0] == 0:
            return
        for i in sel:
            self.files[i - 1], self.files[i] = self.files[i], self.files[i - 1]
            txt = self.listbox.get(i)
            self.listbox.delete(i)
            self.listbox.insert(i - 1, txt)
            self.listbox.selection_set(i - 1)

    def _move_down(self):
        sel = list(self.listbox.curselection())
        if not sel or sel[-1] >= self.listbox.size() - 1:
            return
        for i in reversed(sel):
            self.files[i], self.files[i + 1] = self.files[i + 1], self.files[i]
            txt = self.listbox.get(i)
            self.listbox.delete(i)
            self.listbox.insert(i + 1, txt)
            self.listbox.selection_set(i + 1)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Salvar arquivo mesclado como...",
            defaultextension=".dxf",
            filetypes=[("Arquivo DXF", "*.dxf")]
        )
        if path:
            self.output_var.set(path)

    def _update_summary(self):
        n = len(self.files)
        if n == 0:
            self.summary_label.config(text="Nenhum arquivo selecionado.")
        else:
            try:
                total_kb = sum(os.path.getsize(f) for f in self.files) / 1024
                size_str = f"{total_kb/1024:.1f} MB" if total_kb > 1024 else f"{total_kb:.0f} KB"
            except OSError:
                size_str = "?"
            self.summary_label.config(
                text=f"{n} arquivo{'s' if n > 1 else ''}\n{size_str} no total"
            )

    def _start_merge(self):
        if not self.files:
            messagebox.showwarning("Sem arquivos",
                                   "Adicione pelo menos um arquivo DXF antes de mesclar.")
            return
        output = self.output_var.get().strip()
        if not output:
            messagebox.showwarning("Arquivo de saída",
                                   "Informe o caminho do arquivo de saída.")
            return

        # Garantir extensão .dxf
        if not output.lower().endswith('.dxf'):
            output += '.dxf'
            self.output_var.set(output)

        self.merge_btn.config(state='disabled')
        self.status_label.config(text="", fg=COLORS['muted'])
        self.progress_var.set(0)
        self.progress_label.config(text="Preparando...")

        files_copy = list(self.files)

        def run():
            try:
                def cb(pct, msg):
                    self.root.after(0, lambda: self.progress_var.set(pct))
                    self.root.after(0, lambda: self.progress_label.config(text=msg))

                merge_dxf_files(files_copy, output, progress_callback=cb)
                self.root.after(0, self._on_success, output)
            except Exception as e:
                import traceback
                err = traceback.format_exc()
                self.root.after(0, self._on_error, str(e), err)

        threading.Thread(target=run, daemon=True).start()

    def _on_success(self, output_path):
        self.merge_btn.config(state='normal')
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        self.status_label.config(
            text=f"✓ Salvo com sucesso\n{os.path.basename(output_path)}\n{size_mb:.1f} MB",
            fg=COLORS['success']
        )
        messagebox.showinfo(
            "Merge concluído!",
            f"Arquivo gerado com sucesso:\n\n{output_path}\n\n"
            f"Tamanho: {size_mb:.1f} MB\n"
            f"Arquivos mesclados: {len(self.files)}"
        )

    def _on_error(self, short_msg, full_trace):
        self.merge_btn.config(state='normal')
        self.progress_var.set(0)
        self.progress_label.config(text="")
        self.status_label.config(text=f"Erro: {short_msg}", fg=COLORS['error'])
        print("ERRO COMPLETO:\n", full_trace, file=sys.stderr)
        messagebox.showerror(
            "Erro no merge",
            f"Ocorreu um erro:\n\n{short_msg}\n\n"
            "Verifique se os arquivos são DXF válidos exportados pelo Revit.\n"
            "Detalhes técnicos foram impressos no console."
        )


# ═══════════════════════════════════════════════════════════════
#  PONTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    root = tk.Tk()
    app = DXFMergerApp(root)
    # Tamanho inicial agradável, centralizado
    w, h = 900, 580
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
    root.mainloop()
