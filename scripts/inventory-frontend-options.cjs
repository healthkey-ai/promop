// Parse source only: never evaluate frontend code or load application modules.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(process.argv[2] || path.join(__dirname, '..'));
const ts = require(require.resolve('typescript', { paths: [path.join(root, 'frontend')] }));
const base = path.join(root, 'frontend/src/components/PatientInfo');
const output = { files: {}, constants: [], controls: [], unresolved: [] };
function literal(node) {
  if (!node) throw Error('missing');
  if (ts.isAsExpression(node) || ts.isParenthesizedExpression(node)) return literal(node.expression);
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (ts.isNumericLiteral(node)) return Number(node.text);
  if (node.kind === ts.SyntaxKind.TrueKeyword) return true;
  if (node.kind === ts.SyntaxKind.FalseKeyword) return false;
  if (node.kind === ts.SyntaxKind.NullKeyword) return null;
  if (ts.isArrayLiteralExpression(node)) return node.elements.map(literal);
  if (ts.isObjectLiteralExpression(node)) return Object.fromEntries(node.properties.map(p => {
    if (!ts.isPropertyAssignment(p)) throw Error('dynamic property');
    return [p.name.text, literal(p.initializer)];
  }));
  throw Error('dynamic expression');
}
function scan(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true }).sort((a,b) => a.name.localeCompare(b.name))) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) { scan(file); continue; }
    if (!/\.tsx?$/.test(file) || /\.(test|spec)\./.test(file)) continue;
    const rel = path.relative(root, file), text = fs.readFileSync(file, 'utf8');
    output.files[rel] = crypto.createHash('sha256').update(text).digest('hex');
    const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
    if (source.parseDiagnostics.length) throw Error(`Cannot parse ${rel}`);
    function visit(node) {
      const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1;
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && /OPTIONS$|STATES$/.test(node.name.text)) {
        try {
          const values = literal(node.initializer);
          if (!Array.isArray(values)) throw Error('options are not an array');
          output.constants.push({ file: rel, line, name: node.name.text, values });
        }
        catch { output.unresolved.push({file: rel, line, name: node.name.text, reason: 'dynamic_constant'}); }
      }
      if (ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node)) {
        const attrs = Object.fromEntries(node.attributes.properties.filter(ts.isJsxAttribute).map(p => [p.name.text, p.initializer]));
        if (attrs.options || (attrs.name && attrs.type)) {
          let field = null, type = null, options = null;
          try { field = literal(attrs.name); } catch {}
          try { type = literal(attrs.type); } catch {}
          const expr = attrs.options && ts.isJsxExpression(attrs.options) ? attrs.options.expression : attrs.options;
          try { options = literal(expr); } catch {}
          let parent = node.parent, component = null;
          while (parent) {
            if (ts.isFunctionDeclaration(parent)) { component = parent.name?.text || null; break; }
            parent = parent.parent;
          }
          output.controls.push({file: rel, line, field, type, component, options,
            expression: expr ? expr.getText(source) : null,
            constant: expr && ts.isIdentifier(expr) ? expr.text : null});
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
}
scan(base);
process.stdout.write(JSON.stringify(output));
