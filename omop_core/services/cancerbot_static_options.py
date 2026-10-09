"""Bounded interpretation of literal option providers, never importing CancerBot.

Only the AST operations below are supported. Database access, arbitrary calls,
mutation, loops and unknown syntax leave a binding unresolved.
"""
import ast
import hashlib


class Unresolved(ValueError):
    pass


class Returned(Exception):
    def __init__(self, value):
        self.value = value


class StaticOptions:
    def __init__(self, source):
        self.source = source
        tree = ast.parse(source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ValueOptions')
        self.methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.constants = {}
        for node in cls.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try:
                    self.constants[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
        self.dependencies = set()
        self.stack = []
        self.steps = 0
        self.diseases = set()

    def resolve(self, expression):
        self.dependencies = set()
        self.steps = 0
        self.diseases = set()
        value = self.expr(ast.parse(expression, mode='eval').body, {})
        if not isinstance(value, dict) or set(value) != {'options'}:
            raise Unresolved('Expected an options envelope.')
        options = value['options']
        if not isinstance(options, list) or any(
            not isinstance(item, dict) or set(item) != {'value', 'label'}
            or not isinstance(item['value'], (str, int, float, bool, type(None)))
            or not isinstance(item['label'], str) for item in options
        ):
            raise Unresolved('Expected scalar option keys and string labels.')
        return options

    def evidence(self):
        return [{'method': name, 'line': self.methods[name].lineno,
                 'sha256': hashlib.sha256(ast.get_source_segment(self.source, self.methods[name]).encode()).hexdigest()}
                for name in sorted(self.dependencies)]

    def provider_models(self):
        return sorted({alias.name for name in self.dependencies
                       for node in ast.walk(self.methods[name])
                       if isinstance(node, ast.ImportFrom) and node.module == 'trials.models'
                       for alias in node.names if alias.name != '*'})

    def trace_dependencies(self, expression):
        """Record source requirements even when an unsupported call cannot run."""
        pending = [ast.parse(expression, mode='eval').body]
        seen = set()
        while pending:
            for node in ast.walk(pending.pop()):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == 'self' and node.attr in self.methods and node.attr not in seen):
                    seen.add(node.attr)
                    pending.append(self.methods[node.attr])
        self.dependencies.update(seen)

    def tick(self):
        self.steps += 1
        if self.steps > 10000:
            raise Unresolved('Static expression budget exceeded.')

    def method(self, name, args):
        if name not in self.methods or name in self.stack or len(self.stack) >= 20:
            raise Unresolved('Unknown or recursive provider.')
        node = self.methods[name]
        if any(not isinstance(d, ast.Name) or d.id not in {'property', 'cached_property', 'staticmethod'}
               for d in node.decorator_list):
            raise Unresolved('Unsupported provider decorator.')
        parameters = [arg.arg for arg in node.args.args]
        if parameters and parameters[0] == 'self':
            parameters = parameters[1:]
        if node.args.vararg or node.args.kwarg or node.args.kwonlyargs or len(args) != len(parameters):
            raise Unresolved('Unsupported provider arguments.')
        self.dependencies.add(name)
        values = dict(zip(parameters, args))
        if isinstance(values.get('disease_code'), str):
            self.diseases.add(values['disease_code'])
        self.stack.append(name)
        try:
            self.statements(node.body, values)
        except Returned as result:
            return result.value
        finally:
            self.stack.pop()
        raise Unresolved('Provider has no explicit return.')

    def statements(self, nodes, env):
        for node in nodes:
            self.tick()
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue  # Docstring, not an executable expression.
            if isinstance(node, ast.Return):
                raise Returned(self.expr(node.value, env))
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                env[node.targets[0].id] = self.expr(node.value, env)
            elif isinstance(node, ast.If):
                self.statements(node.body if self.expr(node.test, env) else node.orelse, env)
            else:
                raise Unresolved(f'Unsupported statement: {type(node).__name__}.')

    def expr(self, node, env):
        self.tick()
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in env:
            return env[node.id]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [self.expr(item, env) for item in node.elts]
            return set(values) if isinstance(node, ast.Set) else values
        if isinstance(node, ast.Dict):
            result = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    other = self.expr(value, env)
                    if not isinstance(other, dict):
                        raise Unresolved('Only dictionaries may be unpacked.')
                    result.update(other)
                else:
                    result[self.expr(key, env)] = self.expr(value, env)
            return result
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'self':
            if node.attr in self.constants:
                return self.constants[node.attr]
            method = self.methods.get(node.attr)
            if method is None or not any(isinstance(d, ast.Name) and d.id in {'property', 'cached_property'}
                                         for d in method.decorator_list):
                raise Unresolved('Only declared properties may be read without a call.')
            return self.method(node.attr, [])
        if isinstance(node, ast.Subscript):
            return self.expr(node.value, env)[self.expr(node.slice, env)]
        if isinstance(node, ast.IfExp):
            return self.expr(node.body if self.expr(node.test, env) else node.orelse, env)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for item in node.values:
                value = self.expr(item, env)
                if value:
                    return value
            return value
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = self.expr(node.left, env), self.expr(node.comparators[0], env)
            if isinstance(node.ops[0], ast.In):
                return left in right
            if isinstance(node.ops[0], ast.Eq):
                return left == right
        if isinstance(node, (ast.DictComp, ast.ListComp)) and len(node.generators) == 1:
            generator = node.generators[0]
            if generator.is_async or generator.ifs:
                raise Unresolved('Filtered comprehensions require explicit reconciliation.')
            result = {} if isinstance(node, ast.DictComp) else []
            for item in self.expr(generator.iter, env):
                local = dict(env)
                target = generator.target
                if isinstance(target, ast.Name):
                    local[target.id] = item
                elif isinstance(target, ast.Tuple) and all(isinstance(n, ast.Name) for n in target.elts):
                    if len(target.elts) != len(item):
                        raise Unresolved('Comprehension tuple shape differs.')
                    local.update(zip((n.id for n in target.elts), item))
                else:
                    raise Unresolved('Unsupported comprehension target.')
                if isinstance(node, ast.DictComp):
                    result[self.expr(node.key, local)] = self.expr(node.value, local)
                else:
                    result.append(self.expr(node.elt, local))
            return result
        if isinstance(node, ast.Call) and not node.keywords:
            args = [self.expr(arg, env) for arg in node.args]
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == 'str' and len(args) == 1:
                return str(args[0])
            if isinstance(fn, ast.Attribute):
                if isinstance(fn.value, ast.Name) and fn.value.id == 'self':
                    return self.method(fn.attr, args)
                receiver = self.expr(fn.value, env)
                if isinstance(receiver, str) and fn.attr == 'upper' and not args:
                    return receiver.upper()
                if isinstance(receiver, dict):
                    if fn.attr == 'items' and not args:
                        return list(receiver.items())
                    if fn.attr == 'get' and len(args) in (1, 2):
                        return receiver.get(*args)
        raise Unresolved(f'Unsupported expression: {type(node).__name__}.')


def reconcile_static_bindings(source, bindings, inventory_row):
    """Add resolved public-list rows while retaining original source fragments."""
    interpreter = StaticOptions(source)
    rows = []
    for binding in bindings:
        if binding['coverage'] != 'requires_live_export':
            continue
        try:
            options = interpreter.resolve(binding['expression'])
        except (Unresolved, KeyError, TypeError, IndexError, ValueError) as exc:
            interpreter.trace_dependencies(binding['expression'])
            binding['static_resolution'] = {'resolved': False, 'reason': str(exc),
                                            'dependencies': interpreter.evidence(),
                                            'provider_models': interpreter.provider_models()}
            exclusions = {'register': ('registers', 'Trial'),
                          'trialPurpose': ('trial_purposes', 'TrialPurpose'),
                          'trialType': ('trial_types', 'TrialType')}
            expected = exclusions.get(binding['option_list'])
            if expected and expected[0] in interpreter.dependencies and expected[1] in interpreter.provider_models():
                binding.update(coverage='excluded_trial_search', disposition='not_applicable', owning_issue='#1223',
                    reason='Trial search metadata, not a patient clinical attribute. Source fragments remain inventoried.')
            continue
        evidence = {'method': 'bounded_static_interpretation', 'dependencies': interpreter.evidence()}
        scope = {'disease': next(iter(interpreter.diseases))} if len(interpreter.diseases) == 1 else None
        public_rows = [inventory_row('cancerbot_static', binding['option_list'], item['value'], item['label'],
            value=item['value'], scope=scope, evidence=[evidence],
            reason='Complete deterministic source options; destination and clinical semantics require review.')
            for item in options]
        binding.update(coverage='covered_by_static_source', source_row_ids=[r['id'] for r in public_rows],
                       static_resolution={'resolved': True, **evidence}, option_count=len(public_rows))
        if scope:
            binding['source_context'] = scope
        rows.extend(public_rows)
    return rows
