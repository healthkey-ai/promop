/**
 * A small PDF 1.4 writer: text in Helvetica, filled and stroked rectangles,
 * lines, and pages. Enough to lay out a report, and no more.
 *
 * Why not a library: every dependency change goes through the Security review
 * gate, and a report needs only the PDF base-14 fonts, which every viewer ships,
 * so nothing has to be embedded. The cost of that choice is the character set:
 * base-14 fonts speak WinAnsi (Windows-1252), which covers Western European
 * text. `toWinAnsi` transliterates what it can (subscripts, accents a font does
 * not have) and prints `?` for the rest, e.g. CJK names.
 *
 * Coordinates are top-left based, in points (1/72 in), the way a layout thinks;
 * the writer flips them to PDF's bottom-left origin.
 */

export type FontName = 'regular' | 'bold';
export type Rgb = [number, number, number];

const BASE_FONT: Record<FontName, string> = { regular: 'Helvetica', bold: 'Helvetica-Bold' };

// Adobe AFM advance widths (1/1000 em) for ASCII 32..126.
const HELVETICA = [
  278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
  556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
  1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
  667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
  333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
  556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
];
const HELVETICA_BOLD = [
  278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
  556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
  975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
  667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
  333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
  611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
];
const WIDTHS: Record<FontName, number[]> = { regular: HELVETICA, bold: HELVETICA_BOLD };

/** Windows-1252 bytes 0x80..0x9F, which differ from Latin-1. */
const CP1252_HIGH: Record<string, number> = {
  '€': 0x80, '‚': 0x82, 'ƒ': 0x83, '„': 0x84, '…': 0x85, '†': 0x86, '‡': 0x87,
  'ˆ': 0x88, '‰': 0x89, 'Š': 0x8a, '‹': 0x8b, 'Œ': 0x8c, 'Ž': 0x8e, '‘': 0x91,
  '’': 0x92, '“': 0x93, '”': 0x94, '•': 0x95, '–': 0x96, '—': 0x97, '˜': 0x98,
  '™': 0x99, 'š': 0x9a, '›': 0x9b, 'œ': 0x9c, 'ž': 0x9e, 'Ÿ': 0x9f,
};

/** Characters the tab labels use that WinAnsi lacks. */
const TRANSLITERATE: Record<string, string> = {
  '₀': '0', '₁': '1', '₂': '2', '₃': '3', '₄': '4', '₅': '5', '₆': '6', '₇': '7', '₈': '8', '₉': '9',
  '⁰': '^0', '⁴': '^4', '⁵': '^5', '⁶': '^6', '⁷': '^7', '⁸': '^8', '⁹': '^9',
  '≥': '>=', '≤': '<=', '≠': '!=', '→': '->', '←': '<-', '−': '-', '‐': '-', '‑': '-',
  ' ': ' ', ' ': ' ', ' ': ' ', '\t': ' ',
};

/** One string as WinAnsi byte values. */
export function toWinAnsi(text: string): number[] {
  const out: number[] = [];
  for (const ch of text) {
    const code = ch.codePointAt(0)!;
    if (code >= 0x20 && code <= 0x7e) { out.push(code); continue; }
    if (code >= 0xa0 && code <= 0xff) { out.push(code); continue; }
    if (CP1252_HIGH[ch] !== undefined) { out.push(CP1252_HIGH[ch]); continue; }
    if (TRANSLITERATE[ch] !== undefined) { out.push(...toWinAnsi(TRANSLITERATE[ch])); continue; }
    if (code < 0x20) continue; // control characters: newlines are split by the caller
    // An accent the font has no precomposed glyph for: keep the base letter.
    const base = ch.normalize('NFD').replace(/[̀-ͯ]/g, '');
    if (base && base !== ch && [...base].every((c) => c.charCodeAt(0) < 0x7f)) {
      out.push(...toWinAnsi(base));
      continue;
    }
    out.push(0x3f); // '?'
  }
  return out;
}

const byteWidth = (byte: number, font: FontName): number => {
  if (byte >= 32 && byte <= 126) return WIDTHS[font][byte - 32];
  if (byte >= 0xc0) {
    // Accented Latin letters advance like their base letter.
    const base = String.fromCharCode(byte).normalize('NFD').charCodeAt(0);
    if (base >= 32 && base <= 126) return WIDTHS[font][base - 32];
  }
  return 556;
};

/** Advance width of `text` at `size` points. */
export function textWidth(text: string, font: FontName, size: number): number {
  return toWinAnsi(text).reduce((sum, b) => sum + byteWidth(b, font), 0) * size / 1000;
}

/** Break `text` into lines no wider than `width`, honouring explicit newlines. */
export function wrapText(text: string, font: FontName, size: number, width: number): string[] {
  const lines: string[] = [];
  for (const paragraph of text.split(/\r?\n/)) {
    let line = '';
    for (const word of paragraph.split(/ +/)) {
      const candidate = line ? `${line} ${word}` : word;
      if (textWidth(candidate, font, size) <= width) { line = candidate; continue; }
      if (line) lines.push(line);
      // A single word wider than the column (a URL, an HGVS string): hard-break it.
      let rest = word;
      while (textWidth(rest, font, size) > width && rest.length > 1) {
        let cut = rest.length - 1;
        while (cut > 1 && textWidth(rest.slice(0, cut), font, size) > width) cut--;
        lines.push(rest.slice(0, cut));
        rest = rest.slice(cut);
      }
      line = rest;
    }
    lines.push(line);
  }
  return lines;
}

const pdfString = (text: string): string =>
  '(' + toWinAnsi(text).map((b) => {
    if (b === 0x28 || b === 0x29 || b === 0x5c) return '\\' + String.fromCharCode(b);
    if (b < 0x20 || b > 0x7e) return '\\' + b.toString(8).padStart(3, '0');
    return String.fromCharCode(b);
  }).join('') + ')';

/**
 * A text string outside a content stream (the Info dictionary). Readers decode
 * those as PDFDocEncoding, not the font's WinAnsi, so 0x80..0x9F would turn an
 * em dash into "Š". UTF-16BE with a byte-order mark is read the same everywhere
 * and keeps the full character set, CJK names included.
 */
const textString = (text: string): string => {
  let hex = 'FEFF';
  for (let i = 0; i < text.length; i++) hex += text.charCodeAt(i).toString(16).padStart(4, '0').toUpperCase();
  return `<${hex}>`;
};

const num = (n: number): string => (Math.round(n * 100) / 100).toString();
const rgb = ([r, g, b]: Rgb): string => [r, g, b].map((c) => num(c / 255)).join(' ');

export interface DocumentInfo { title?: string; author?: string; subject?: string }

export class PdfWriter {
  readonly width: number;
  readonly height: number;
  private pages: string[][] = [];

  constructor(width = 612, height = 792) {
    this.width = width;
    this.height = height;
  }

  get pageCount(): number { return this.pages.length; }

  addPage(): number {
    this.pages.push([]);
    return this.pages.length - 1;
  }

  private ops(page?: number): string[] {
    if (!this.pages.length) this.addPage();
    return this.pages[page ?? this.pages.length - 1];
  }

  /** `y` is the text baseline, measured from the top of the page. */
  text(x: number, y: number, text: string, opts: { font?: FontName; size?: number; color?: Rgb; page?: number } = {}) {
    const { font = 'regular', size = 10, color = [0, 0, 0] } = opts;
    this.ops(opts.page).push(
      `BT /${font === 'bold' ? 'F2' : 'F1'} ${num(size)} Tf ${rgb(color)} rg `
      + `${num(x)} ${num(this.height - y)} Td ${pdfString(text)} Tj ET`,
    );
  }

  /** `y` is the top edge. */
  rect(x: number, y: number, w: number, h: number, opts: { fill?: Rgb; stroke?: Rgb; lineWidth?: number; page?: number }) {
    const parts = [`${num(x)} ${num(this.height - y - h)} ${num(w)} ${num(h)} re`];
    if (opts.fill) parts.unshift(`${rgb(opts.fill)} rg`);
    if (opts.stroke) parts.unshift(`${rgb(opts.stroke)} RG ${num(opts.lineWidth ?? 0.5)} w`);
    parts.push(opts.fill && opts.stroke ? 'B' : opts.fill ? 'f' : 'S');
    this.ops(opts.page).push(parts.join(' '));
  }

  line(x1: number, y1: number, x2: number, y2: number, opts: { color?: Rgb; lineWidth?: number; page?: number } = {}) {
    this.ops(opts.page).push(
      `${rgb(opts.color ?? [0, 0, 0])} RG ${num(opts.lineWidth ?? 0.5)} w `
      + `${num(x1)} ${num(this.height - y1)} m ${num(x2)} ${num(this.height - y2)} l S`,
    );
  }

  /**
   * The finished file. Every character is below U+0100 (content is ASCII with
   * octal escapes), so string offsets are byte offsets.
   */
  toBytes(info: DocumentInfo = {}): Uint8Array {
    if (!this.pages.length) this.addPage();
    const objects: string[] = [];
    // push() returns the new length, which is the 1-based object number.
    const add = (body: string) => objects.push(body);

    const catalog = add('');
    const pagesObj = add('');
    const font = (name: FontName) =>
      add(`<< /Type /Font /Subtype /Type1 /BaseFont /${BASE_FONT[name]} /Encoding /WinAnsiEncoding >>`);
    const fontRegular = font('regular');
    const fontBold = font('bold');
    const kids: number[] = [];
    for (const ops of this.pages) {
      const stream = ops.join('\n');
      const content = add(`<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`);
      kids.push(add(
        `<< /Type /Page /Parent ${pagesObj} 0 R /MediaBox [0 0 ${this.width} ${this.height}] `
        + `/Resources << /Font << /F1 ${fontRegular} 0 R /F2 ${fontBold} 0 R >> >> /Contents ${content} 0 R >>`,
      ));
    }
    objects[catalog - 1] = `<< /Type /Catalog /Pages ${pagesObj} 0 R >>`;
    objects[pagesObj - 1] = `<< /Type /Pages /Kids [${kids.map((k) => `${k} 0 R`).join(' ')}] /Count ${kids.length} >>`;
    const infoEntries = Object.entries({ Title: info.title, Author: info.author, Subject: info.subject, Producer: 'HealthKey' })
      .filter(([, v]) => v)
      .map(([k, v]) => `/${k} ${textString(v as string)}`);
    const infoObj = add(`<< ${infoEntries.join(' ')} >>`);

    // The comment's high bytes mark the file as binary to transfer tools.
    let out = '%PDF-1.4\n%\xe2\xe3\xcf\xd3\n';
    const offsets: number[] = [];
    objects.forEach((body, i) => {
      offsets.push(out.length);
      out += `${i + 1} 0 obj\n${body}\nendobj\n`;
    });
    const xref = out.length;
    out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
    out += offsets.map((o) => `${String(o).padStart(10, '0')} 00000 n \n`).join('');
    out += `trailer\n<< /Size ${objects.length + 1} /Root ${catalog} 0 R /Info ${infoObj} 0 R >>\nstartxref\n${xref}\n%%EOF\n`;

    const bytes = new Uint8Array(out.length);
    for (let i = 0; i < out.length; i++) bytes[i] = out.charCodeAt(i);
    return bytes;
  }
}
