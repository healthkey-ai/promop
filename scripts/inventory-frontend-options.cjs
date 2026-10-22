// Parse source only: never evaluate frontend code or load application modules.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(process.argv[2] || path.join(__dirname, '..'));
const ts = require(require.resolve('typescript', { paths: [path.join(root, 'frontend')] }));
const base = path.join(root, 'frontend/src/components/PatientInfo');
const output = { files: {}, constants: [], controls: [], unresolved: [] };
const structuredConstants = {
  'frontend/src/components/PatientInfo/GelfAssessment.tsx': { FACTORS: 'gelf_criteria_options' },
  'frontend/src/components/PatientInfo/flipiFactors.ts': { FLIPI_FACTORS: 'flipi_score_options' },
};
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
    if (!ts.isPropertyAssignment(p) || ts.isComputedPropertyName(p.name)) throw Error('dynamic property');
    return [p.name.text, literal(p.initializer)];
  }));
  throw Error('dynamic expression');
}
function isFieldHelper(node) {
  if (!ts.isVariableDeclaration(node) || node.name.text !== 'field'
      || !node.initializer || !ts.isArrowFunction(node.initializer)) return false;
  const arrow = node.initializer;
  if (arrow.parameters.map(p => p.name.text).join(',') !== 'label,name,type,extra'
      || !ts.isBlock(arrow.body)) return false;
  const returns = arrow.body.statements.filter(ts.isReturnStatement);
  if (returns.length !== 1) return false;
  let result = returns[0].expression;
  while (result && ts.isParenthesizedExpression(result)) result = result.expression;
  if (!result || !ts.isJsxSelfClosingElement(result) || result.tagName.getText() !== 'ClinicalField') return false;
  const attrs = Object.fromEntries(result.attributes.properties.filter(ts.isJsxAttribute)
    .map(p => [p.name.text, p.initializer]));
  const name = attrs.name?.expression, type = attrs.type?.expression, options = attrs.options?.expression;
  return name && ts.isIdentifier(name) && name.text === 'name'
    && type && ts.isIdentifier(type) && type.text === 'type'
    && options && ts.isPropertyAccessExpression(options) && options.expression.getText() === 'extra'
    && options.name.text === 'options';
}
function optionGroups(value, parents = []) {
  if (Array.isArray(value)) return [{ parents, values: value }];
  if (!value || typeof value !== 'object') throw Error('options are not arrays');
  return Object.entries(value).flatMap(([key, child]) => optionGroups(child, [...parents, key]));
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
    let hasFieldHelper = false;
    function findHelper(node) {
      if (isFieldHelper(node)) hasFieldHelper = true;
      ts.forEachChild(node, findHelper);
    }
    if (rel === 'frontend/src/components/PatientInfo/tabs/GeneralTab.tsx') findHelper(source);
    function visit(node) {
      const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1;
      const structuredField = ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)
        ? structuredConstants[rel]?.[node.name.text] : null;
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && (/OPTIONS$|STATES$/.test(node.name.text) || structuredField)) {
        try {
          let values = literal(node.initializer);
          if (structuredField) {
            if (!Array.isArray(values) || values.some(v => !Array.isArray(v) || v.length !== 2 || v.some(x => typeof x !== 'string'))) throw Error('unrecognized criterion definition');
            values = values.map(([value, label]) => ({ value, label }));
          }
          for (const group of optionGroups(values)) {
            output.constants.push({ file: rel, line,
              name: node.name.text + group.parents.map(p => `[${JSON.stringify(p)}]`).join(''),
              ...(structuredField ? {destination_field: structuredField, disease: 'FL'} : {}),
              root_name: node.name.text, parent_keys: group.parents, values: group.values });
          }
        }
        catch { output.unresolved.push({file: rel, line, name: node.name.text, reason: 'dynamic_constant'}); }
      }
      // This reviewed UI helper renders ClinicalField with the supplied literal
      // name/type and extra.options. Parse its call sites, never execute it.
      if (rel === 'frontend/src/components/PatientInfo/tabs/GeneralTab.tsx'
          && hasFieldHelper
          && ts.isCallExpression(node) && ts.isIdentifier(node.expression)
          && node.expression.text === 'field' && node.arguments.length >= 3) {
        let field = null, type = null, options = null, expr = null;
        try { field = literal(node.arguments[1]); type = literal(node.arguments[2]); } catch {}
        const extra = node.arguments[3];
        if (extra && ts.isObjectLiteralExpression(extra)) {
          expr = extra.properties.find(p => ts.isPropertyAssignment(p) && p.name.text === 'options')?.initializer;
          try { options = literal(expr); } catch {}
        }
        else if (extra) expr = extra;
        output.controls.push({ file: rel, line, field, type, component: 'GeneralTab', options,
          expression: expr ? expr.getText(source) : null,
          constant: expr && ts.isIdentifier(expr) ? expr.text : null,
          helper: 'field' });
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
