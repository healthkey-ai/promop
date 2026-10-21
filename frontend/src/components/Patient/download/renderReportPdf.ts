/**
 * Lay a `PatientReport` out on US Letter pages.
 *
 * One flowing document rather than a page per tab: most tabs hold a handful of
 * values for a given patient, and a page each would print mostly white. Each
 * tab gets a heading, each section a band, fields a two-column grid of label
 * over value, and lists a table whose header repeats on a page break.
 */
import { PdfWriter, type FontName, type Rgb, textWidth, wrapText } from './pdfWriter';
import type { PatientReport, ReportField, ReportSection, ReportTable } from './patientReport';

const MARGIN = 48;
/** Sections up to this tall are never split across pages. */
const KEEP_TOGETHER = 240;
const FOOTER = 36;
const BRAND: Rgb = [79, 70, 229];
const INK: Rgb = [17, 24, 39];
const MUTED: Rgb = [107, 114, 128];
const RULE: Rgb = [229, 231, 235];
const BAND: Rgb = [243, 244, 246];
const ZEBRA: Rgb = [249, 250, 251];

const LABEL = { font: 'regular' as FontName, size: 7.5, lead: 9.5 };
const VALUE = { font: 'regular' as FontName, size: 10, lead: 12.5 };
const CELL = { font: 'regular' as FontName, size: 8.5, lead: 10.5 };
const HEAD = { font: 'bold' as FontName, size: 8, lead: 10 };

class Layout {
  readonly pdf = new PdfWriter();
  private y = 0;
  readonly left = MARGIN;
  readonly right = this.pdf.width - MARGIN;
  get width() { return this.right - this.left; }
  private get bottom() { return this.pdf.height - MARGIN - FOOTER; }
  /** The usable height of a fresh page. */
  private get pageArea() { return this.bottom - MARGIN; }

  constructor() { this.newPage(); }

  newPage() {
    this.pdf.addPage();
    this.y = MARGIN;
  }

  /** Start a new page unless `height` more points still fit on this one. */
  ensure(height: number) {
    if (this.y + height > this.bottom) this.newPage();
  }

  header(report: PatientReport) {
    const { pdf } = this;
    pdf.rect(0, 0, pdf.width, 6, { fill: BRAND });
    pdf.text(this.left, this.y + 10, 'PATIENT HEALTH RECORD', { font: 'bold', size: 8, color: BRAND });
    const generated = report.generatedAt.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    const stamp = `Generated ${generated}`;
    pdf.text(this.right - textWidth(stamp, 'regular', 8), this.y + 10, stamp, { size: 8, color: MUTED });
    this.y += 16;
    for (const line of wrapText(report.name || `Patient #${report.personId}`, 'bold', 20, this.width)) {
      this.y += 22;
      pdf.text(this.left, this.y, line, { font: 'bold', size: 20, color: INK });
    }
    this.y += 14;

    // The summary strip: number, birth date, gender, disease.
    const cols = Math.max(1, report.summary.length);
    const colWidth = this.width / cols;
    const boxTop = this.y;
    const valueLines = report.summary.map((f) => wrapText(f.value, 'bold', 9.5, colWidth - 16));
    const boxHeight = 18 + Math.max(1, ...valueLines.map((l) => l.length)) * 12;
    pdf.rect(this.left, boxTop, this.width, boxHeight, { fill: BAND });
    report.summary.forEach((f, i) => {
      const x = this.left + 10 + i * colWidth;
      pdf.text(x, boxTop + 13, f.label.toUpperCase(), { size: 6.5, color: MUTED });
      valueLines[i].forEach((line, j) => pdf.text(x, boxTop + 25 + j * 12, line, { font: 'bold', size: 9.5, color: INK }));
    });
    this.y = boxTop + boxHeight + 10;
  }

  tabHeading(title: string, firstBlockHeight: number) {
    // Keep a heading with what it heads: the whole first section when that is
    // short enough to be kept together, otherwise its first few rows.
    this.ensure(34 + (firstBlockHeight <= KEEP_TOGETHER ? firstBlockHeight : 80));
    for (const line of wrapText(title, 'bold', 14, this.width)) {
      this.y += 20;
      this.pdf.text(this.left, this.y, line, { font: 'bold', size: 14, color: BRAND });
    }
    this.y += 6;
    this.pdf.line(this.left, this.y, this.right, this.y, { color: BRAND, lineWidth: 1 });
    this.y += 8;
  }

  sectionHeading(title: string) {
    this.ensure(18 + 30);
    this.pdf.rect(this.left, this.y, this.width, 16, { fill: BAND });
    this.pdf.text(this.left + 6, this.y + 11, title, { font: 'bold', size: 9, color: INK });
    this.y += 22;
  }

  note(text: string) {
    const lines = wrapText(text, 'regular', 9, this.width);
    this.ensure(lines.length * 12);
    for (const line of lines) {
      this.y += 10;
      this.pdf.text(this.left + 6, this.y, line, { size: 9, color: MUTED });
      this.y += 2;
    }
    this.y += 6;
  }

  private fieldRows(fields: ReportField[]) {
    const gutter = 18;
    const colWidth = (this.width - gutter) / 2 - 6;
    const rows = [];
    for (let i = 0; i < fields.length; i += 2) {
      const cells = fields.slice(i, i + 2).map((f) => ({
        label: wrapText(f.label, LABEL.font, LABEL.size, colWidth),
        value: wrapText(f.value, VALUE.font, VALUE.size, colWidth),
      }));
      const height = Math.max(...cells.map((c) => c.label.length * LABEL.lead + c.value.length * VALUE.lead)) + 7;
      rows.push({ cells, height });
    }
    return { rows, colWidth, gutter, height: rows.reduce((sum, r) => sum + r.height, 0) + 4 };
  }

  /** Two columns of label over value; a row is as tall as its taller cell. */
  fields(fields: ReportField[]) {
    const { rows, colWidth, gutter } = this.fieldRows(fields);
    for (const { cells, height } of rows) {
      if (height > this.pageArea) {
        // Taller than a page (a very long free-text value): give up the second
        // column and let the lines flow onto as many pages as they need.
        for (const cell of cells) {
          for (const line of cell.label) {
            this.ensure(LABEL.lead);
            this.y += LABEL.lead;
            this.pdf.text(this.left + 6, this.y - 2, line, { size: LABEL.size, color: MUTED });
          }
          for (const line of cell.value) {
            this.ensure(VALUE.lead);
            this.y += VALUE.lead;
            this.pdf.text(this.left + 6, this.y - 2, line, { size: VALUE.size, color: INK });
          }
          this.y += 7;
        }
        continue;
      }
      this.ensure(height);
      cells.forEach((cell, col) => {
        const x = this.left + 6 + col * (colWidth + gutter + 6);
        let y = this.y;
        for (const line of cell.label) { y += LABEL.lead; this.pdf.text(x, y - 2, line, { size: LABEL.size, color: MUTED }); }
        for (const line of cell.value) { y += VALUE.lead; this.pdf.text(x, y - 2, line, { size: VALUE.size, color: INK }); }
      });
      this.y += height;
    }
    this.y += 4;
  }

  /**
   * Height of a section, so a short one moves to the next page whole rather
   * than leaving its last field stranded under the next page's top margin.
   * A table's height is not worth measuring: its header repeats when it breaks.
   */
  sectionHeight(section: ReportSection): number {
    const fields = section.fields?.length ? this.fieldRows(section.fields).height : 0;
    return 22 + fields + (section.note ? 18 : 0) + (section.table ? 40 : 0);
  }

  /**
   * Column widths follow content, within limits, so a long regimen name takes
   * the room a two-digit line number does not need.
   */
  table(table: ReportTable) {
    const pad = 4;
    const natural = table.columns.map((c, i) => Math.max(
      textWidth(c, HEAD.font, HEAD.size),
      ...table.rows.map((r) => textWidth(r[i] ?? '', CELL.font, CELL.size)),
    ) + pad * 2);
    // No column narrower than its longest word, so "OUTCOME" never breaks into
    // "OUTCOM / E". Capped, so one long HGVS string cannot starve the others;
    // past the cap a word is hard-broken.
    const longestWord = (text: string, style: typeof CELL) =>
      Math.max(0, ...text.split(/\s+/).map((w) => textWidth(w, style.font, style.size)));
    let floors = table.columns.map((c, i) => Math.max(
      28,
      longestWord(c.toUpperCase(), HEAD) + pad * 2,
      Math.min(110, Math.max(0, ...table.rows.map((r) => longestWord(r[i] ?? '', CELL))) + pad * 2),
    ));
    const floorTotal = floors.reduce((a, b) => a + b, 0);
    if (floorTotal > this.width) floors = floors.map((f) => (f / floorTotal) * this.width);
    let widths = natural.map((w, i) => Math.max(floors[i], w));
    const total = widths.reduce((a, b) => a + b, 0);
    if (total > this.width) {
      // Shrink in proportion to how much each column wants beyond its floor.
      const want = widths.map((w, i) => w - floors[i]);
      const spare = this.width - floors.reduce((a, b) => a + b, 0);
      const sum = want.reduce((a, b) => a + b, 0) || 1;
      widths = want.map((w, i) => floors[i] + (w / sum) * Math.max(0, spare));
    } else {
      const extra = (this.width - widths.reduce((a, b) => a + b, 0)) / widths.length;
      widths = widths.map((w) => w + extra);
    }
    const xs = widths.reduce<number[]>((acc, _w, i) => [...acc, i ? acc[i - 1] + widths[i - 1] : this.left], []);

    const drawRow = (cells: string[], style: typeof CELL, fill?: Rgb) => {
      const wrapped = cells.map((c, i) => wrapText(c ?? '', style.font, style.size, widths[i] - pad * 2));
      const lineCount = Math.max(1, ...wrapped.map((w) => w.length));
      const heightOf = (lines: number) => lines * style.lead + pad * 2;
      /** Draw lines [from, to) of every cell as one band. */
      const band = (from: number, to: number) => {
        const height = heightOf(to - from);
        if (fill) this.pdf.rect(this.left, this.y, this.width, height, { fill });
        wrapped.forEach((lines, i) => lines.slice(from, to).forEach((line, j) => this.pdf.text(
          xs[i] + pad, this.y + pad + (j + 1) * style.lead - 2.5, line,
          { font: style.font, size: style.size, color: style === HEAD ? MUTED : INK },
        )));
        this.y += height;
        this.pdf.line(this.left, this.y, this.right, this.y, { color: RULE });
      };
      return { height: heightOf(lineCount), lineCount, heightOf, band };
    };

    const header = () => drawRow(table.columns.map((c) => c.toUpperCase()), HEAD, BAND);
    const first = header();
    this.ensure(first.height + Math.min(drawRow(table.rows[0] ?? [], CELL).height, 60));
    first.band(0, first.lineCount);
    table.rows.forEach((row, i) => {
      const r = drawRow(row, CELL, i % 2 ? ZEBRA : undefined);
      if (this.y + r.height > this.bottom && r.height <= this.pageArea - first.height) {
        this.newPage();
        first.band(0, first.lineCount);
      }
      // A row taller than a page continues on the next, under a repeated header.
      let from = 0;
      while (from < r.lineCount) {
        let fit = Math.floor((this.bottom - this.y - 2 * pad) / CELL.lead);
        if (fit < 1) {
          this.newPage();
          first.band(0, first.lineCount);
          fit = Math.floor((this.bottom - this.y - 2 * pad) / CELL.lead);
        }
        const to = Math.min(r.lineCount, from + Math.max(1, fit));
        r.band(from, to);
        from = to;
      }
    });
    this.y += 10;
  }

  section(section: ReportSection) {
    const height = this.sectionHeight(section);
    if (height <= KEEP_TOGETHER) this.ensure(height);
    this.sectionHeading(section.title);
    if (section.note) this.note(section.note);
    if (section.fields?.length) this.fields(section.fields);
    if (section.table) this.table(section.table);
  }

  footers(report: PatientReport) {
    const total = this.pdf.pageCount;
    // Shortened to its share of the line, or a long name overprints the notice.
    const room = (this.width - textWidth('Confidential health information', 'regular', 7.5)) / 2 - 12;
    let who = `${report.name || 'Patient'}  ·  #${report.personId}`;
    if (textWidth(who, 'regular', 7.5) > room) {
      let name = report.name;
      while (name.length > 1 && textWidth(`${name}…  ·  #${report.personId}`, 'regular', 7.5) > room) name = name.slice(0, -1);
      who = `${name}…  ·  #${report.personId}`;
    }
    for (let page = 0; page < total; page++) {
      const y = this.pdf.height - MARGIN + 6;
      this.pdf.line(this.left, y - 14, this.right, y - 14, { color: RULE, page });
      this.pdf.text(this.left, y, who, { size: 7.5, color: MUTED, page });
      const label = `Page ${page + 1} of ${total}`;
      this.pdf.text(this.right - textWidth(label, 'regular', 7.5), y, label, { size: 7.5, color: MUTED, page });
      const notice = 'Confidential health information';
      this.pdf.text((this.pdf.width - textWidth(notice, 'regular', 7.5)) / 2, y, notice, { size: 7.5, color: MUTED, page });
    }
  }
}

export function renderReportPdf(report: PatientReport): Uint8Array {
  const layout = new Layout();
  layout.header(report);
  for (const tab of report.tabs) {
    layout.tabHeading(tab.title, tab.sections[0] ? layout.sectionHeight(tab.sections[0]) : 20);
    if (!tab.sections.length) {
      layout.note('No information recorded.');
      continue;
    }
    tab.sections.forEach((s) => layout.section(s));
  }
  layout.footers(report);
  return layout.pdf.toBytes({
    title: `Health record — ${report.name || `Patient #${report.personId}`}`,
    subject: 'Patient health record',
  });
}
