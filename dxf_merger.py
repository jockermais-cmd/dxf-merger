"""
DXF Merger v1.0
Unificador de pranchas DXF exportadas do Revit

Uso: python dxf_merger.py
Requisitos: Python 3.8+ com tkinter (incluso no Python padrão para Windows e macOS)
Sem dependências externas.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import sys
import threading
from pathlib import Path
from collections import Counter


# ═══════════════════════════════════════════════════════════════
#  CORE — Lógica de leitura e merge de DXF (sem dependências)
# ═══════════════════════════════════════════════════════════════

def parse_dxf_pairs(filepath):
    """Lê o DXF e retorna lista de (group_code, value)."""
    with open(filepath, 'r', encoding='latin-1', errors='replace') as f:
        lines = [l.rstrip('\r\n') for l in f.readlines()]
    pairs = []
    for i in range(0, len(lines) - 1, 2):
        pairs.append((lines[i].strip(), lines[i + 1].strip()))
    return pairs


def extract_sections(pairs):
    """Extrai seções HEADER, TABLES, ENTITIES do DXF."""
    sections = {}
    i = 0
    while i < len(pairs):
        if pairs[i] == ('0', 'SECTION') and i + 1 < len(pairs):
            name = pairs[i + 1][1]
            start = i + 2
            j = start
            while j < len(pairs):
                if pairs[j] == ('0', 'ENDSEC'):
                    sections[name] = pairs[start:j]
                    i = j + 1
                    break
                j += 1
        i += 1
    return sections


def get_table_records(tables_pairs, entity_type):
    """Extrai registros de um tipo de tabela, retornando {nome: pares}."""
    records = {}
    i = 0
    while i < len(tables_pairs):
        if tables_pairs[i][0] == '0' and tables_pairs[i][1] == entity_type:
            start = i
            name = None
            j = i + 1
            while j < len(tables_pairs) and tables_pairs[j][0] != '0':
                if tables_pairs[j][0] == '2':
                    name = tables_pairs[j][1]
                j += 1
            if name and name not in records:
                records[name] = tables_pairs[start:j]
            i = j
        else:
            i += 1
    return records


def split_entities(entities_pairs):
    """
    Separa entidades em modelspace e paperspace.
    Group code 67 == '1' indica paperspace; ausente ou '0' indica modelspace.
    """
    model_ents, paper_ents = [], []
    i = 0
    while i < len(entities_pairs):
        if entities_pairs[i][0] == '0':
            start = i
            j = i + 1
            space = 'model'
            while j < len(entities_pairs) and entities_pairs[j][0] != '0':
                if entities_pairs[j][0] == '67' and entities_pairs[j][1] == '1':
                    space = 'paper'
                j += 1
            ent = entities_pairs[start:j]
            (model_ents if space == 'model' else paper_ents).append(ent)
            i = j
        else:
            i += 1
    return model_ents, paper_ents


def compute_bbox(ents):
    """Calcula bounding box das entidades. Retorna (xmin, ymin, xmax, ymax)."""
    xs, ys = [], []
    for ent in ents:
        for c, v in ent:
            try:
                if c in ('10', '11', '12', '13', '14', '15', '16'):
                    xs.append(float(v))
                elif c in ('20', '21', '22', '23', '24', '25', '26'):
                    ys.append(float(v))
            except ValueError:
                pass
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


def pairs_to_text(pairs):
    """Converte lista de pares de volta para texto DXF."""
    return '\n'.join(f"{c:>3}\n{v}" for c, v in pairs)


def translate_entity_x(ent_pairs, ox):
    """Translada coordenadas X de uma entidade pelo offset ox."""
    result = []
    for c, v in ent_pairs:
        if c in ('10', '11', '12', '13', '14', '15', '16'):
            try:
                result.append((c, f"{float(v) + ox:.10g}"))
            except ValueError:
                result.append((c, v))
        else:
            result.append((c, v))
    return result


def translate_entity_y(ent_pairs, oy, skip_code22=False):
    """Translada coordenadas Y de uma entidade pelo offset oy."""
    result = []
    for c, v in ent_pairs:
        # code 22 no VIEWPORT é a componente Z do view center, não Y do paper
        if c in ('20', '21', '23', '24', '25', '26') or (c == '22' and not skip_code22):
            try:
                result.append((c, f"{float(v) + oy:.10g}"))
            except ValueError:
                result.append((c, v))
        else:
            result.append((c, v))
    return result


def merge_dxf_files(input_files, output_path, progress_callback=None):
    """
    Mescla múltiplos DXF exportados pelo Revit em um único arquivo.

    Cada arquivo de entrada contribui com:
      - Suas entidades de modelspace, transladadas em X para não se sobreporem
      - Suas entidades de paperspace (carimbo + viewports), transladadas em Y
      - A viewport é ajustada para apontar para a posição correta no modelspace unificado

    Camadas, linetypes e estilos são fundidos sem duplicação.
    O nome do arquivo é usado como prefixo para garantir unicidade dos layouts.
    """

    def cb(pct, msg):
        if progress_callback:
            progress_callback(pct, msg)

    cb(0, "Iniciando...")

    # ── 1. Carregar todos os arquivos ──────────────────────────────────────
    parsed = []
    for i, fpath in enumerate(input_files):
        cb(int(i / len(input_files) * 20), f"Lendo {os.path.basename(fpath)}...")
        pairs = parse_dxf_pairs(fpath)
        sections = extract_sections(pairs)
        parsed.append({
            'path': fpath,
            'name': Path(fpath).stem,
            'sections': sections,
        })

    # ── 2. Fundir tabelas (camadas, estilos, tipos de linha) ───────────────
    cb(25, "Fundindo camadas e estilos...")
    merged_layers    = {}
    merged_ltypes    = {}
    merged_styles    = {}
    merged_dimstyles = {}

    for pf in parsed:
        tables = pf['sections'].get('TABLES', [])
        merged_layers.update(get_table_records(tables, 'LAYER'))
        merged_ltypes.update(get_table_records(tables, 'LTYPE'))
        merged_styles.update(get_table_records(tables, 'STYLE'))
        merged_dimstyles.update(get_table_records(tables, 'DIMSTYLE'))

    # ── 3. Separar entidades e calcular offsets ───────────────────────────
    cb(35, "Calculando posicionamento das pranchas...")

    X_GAP_MODEL  = 50.0   # gap entre modelos no modelspace unificado (metros)
    Y_GAP_PAPER  = 20.0   # gap entre pranchas no paperspace unificado

    file_data = []
    x_offset = 0.0

    for pf in parsed:
        ents = pf['sections'].get('ENTITIES', [])
        model_ents, paper_ents = split_entities(ents)
        model_bb = compute_bbox(model_ents)
        paper_bb = compute_bbox(paper_ents)
        model_width = max(model_bb[2] - model_bb[0], 1.0)

        file_data.append({
            'name': pf['name'],
            'model_ents': model_ents,
            'paper_ents': paper_ents,
            'model_bb':   model_bb,
            'paper_bb':   paper_bb,
            'x_offset':   x_offset,      # posição X no modelspace unificado
        })
        x_offset += model_width + X_GAP_MODEL

    # ── 4. Construir o DXF de saída ───────────────────────────────────────
    cb(50, "Construindo seção HEADER...")
    out = []

    # ─ HEADER: reutilizar do primeiro arquivo ─────────────────────────────
    first_header = parsed[0]['sections'].get('HEADER', [])
    out.append("  0\nSECTION\n  2\nHEADER")
    if first_header:
        out.append(pairs_to_text(first_header))
    out.append("  0\nENDSEC")

    # ─ TABLES ─────────────────────────────────────────────────────────────
    cb(55, "Construindo seção TABLES...")
    out.append("  0\nSECTION\n  2\nTABLES")

    # VPORT (mínima)
    out.append("  0\nTABLE\n  2\nVPORT\n 70\n     0\n  0\nENDTAB")

    # LTYPE
    out.append(f"  0\nTABLE\n  2\nLTYPE\n 70\n{len(merged_ltypes):6}")
    for pairs in merged_ltypes.values():
        out.append(pairs_to_text(pairs))
    out.append("  0\nENDTAB")

    # LAYER
    out.append(f"  0\nTABLE\n  2\nLAYER\n 70\n{len(merged_layers):6}")
    for pairs in merged_layers.values():
        out.append(pairs_to_text(pairs))
    out.append("  0\nENDTAB")

    # STYLE
    out.append(f"  0\nTABLE\n  2\nSTYLE\n 70\n{len(merged_styles):6}")
    for pairs in merged_styles.values():
        out.append(pairs_to_text(pairs))
    out.append("  0\nENDTAB")

    # DIMSTYLE
    out.append(f"  0\nTABLE\n  2\nDIMSTYLE\n 70\n{len(merged_dimstyles):6}")
    for pairs in merged_dimstyles.values():
        out.append(pairs_to_text(pairs))
    out.append("  0\nENDTAB")

    # BLOCK_RECORD: apenas os espaços principais (AutoCAD exige)
    out.append("  0\nTABLE\n  2\nBLOCK_RECORD\n 70\n     2")
    out.append("  0\nBLOCK_RECORD\n  5\n1\n"
               "100\nAcDbSymbolTableRecord\n100\nAcDbBlockTableRecord\n"
               "  2\n*Model_Space\n340\n0")
    out.append("  0\nBLOCK_RECORD\n  5\n2\n"
               "100\nAcDbSymbolTableRecord\n100\nAcDbBlockTableRecord\n"
               "  2\n*Paper_Space\n340\n0")
    out.append("  0\nENDTAB")

    out.append("  0\nENDSEC")  # fim TABLES

    # ─ ENTITIES ───────────────────────────────────────────────────────────
    cb(65, "Mesclando entidades do modelspace...")
    out.append("  0\nSECTION\n  2\nENTITIES")

    # Modelspace: todas as entidades de todos os arquivos, com offset X
    for fd in file_data:
        ox = fd['x_offset'] - fd['model_bb'][0]  # alinhar início no x_offset
        for ent in fd['model_ents']:
            translated = translate_entity_x(ent, ox)
            out.append(pairs_to_text(translated))

    cb(80, "Mesclando entidades do paperspace...")

    # Paperspace: empilhar verticalmente, ajustando viewports
    paper_y_cursor = 0.0

    for fd in file_data:
        pbb = fd['paper_bb']
        y_min = pbb[1]
        paper_height = max(pbb[3] - pbb[1], 10.0)
        y_shift = paper_y_cursor - y_min  # deslocar para começar em paper_y_cursor

        # Offset X do modelo para este arquivo
        model_ox = fd['x_offset'] - fd['model_bb'][0]

        for ent in fd['paper_ents']:
            entity_type = ent[0][1] if ent else ''

            if entity_type == 'VIEWPORT':
                # Viewport: transladar posição no paper (Y) e
                # ajustar o center da view no modelspace (code 12 = X)
                new_ent = []
                for c, v in ent:
                    if c == '20':  # pos Y no paperspace → deslocar
                        try:
                            new_ent.append((c, f"{float(v) + y_shift:.10g}"))
                        except ValueError:
                            new_ent.append((c, v))
                    elif c == '12':  # view center X no modelspace → adicionar offset do modelo
                        try:
                            new_ent.append((c, f"{float(v) + model_ox:.10g}"))
                        except ValueError:
                            new_ent.append((c, v))
                    else:
                        new_ent.append((c, v))
                out.append(pairs_to_text(new_ent))
            else:
                # Carimbo e outras entidades do paper: só deslocar em Y
                translated = translate_entity_y(ent, y_shift)
                out.append(pairs_to_text(translated))

        paper_y_cursor += paper_height + Y_GAP_PAPER

    out.append("  0\nENDSEC")
    out.append("  0\nEOF")

    # ── 5. Escrever arquivo de saída ───────────────────────────────────────
    cb(92, "Escrevendo arquivo DXF...")
    with open(output_path, 'w', encoding='latin-1', errors='replace') as f:
        f.write('\n'.join(out))

    n = len(file_data)
    cb(100, f"✓ Concluído! {n} arquivo{'s' if n > 1 else ''} mesclado{'s' if n > 1 else ''}.")
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
        self.root.title("DXF Merger — Unificador de Pranchas Revit")
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

        try:
            s.configure('TProgressbar',
                        troughcolor=COLORS['border_light'],
                        background=COLORS['accent'],
                        thickness=8)
        except Exception:
            pass

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
            maximum=100
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
