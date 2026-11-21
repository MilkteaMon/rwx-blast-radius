"""Refine the most expensive tests using only pinned-source observations."""
import ast
import json
from pathlib import Path
import re

from selector_v2 import apply_file


def edited_groups(chunk):
    """Return original and replacement code line numbers for each adjacent edit."""
    groups = []
    left = right = None
    before, after = [], []
    for line in chunk.splitlines()+[" "]:
        if line.startswith("@@ ") or line.startswith(" "):
            if before or after:
                groups.append((before, after))
                before, after = [], []
            if line.startswith("@@ "):
                m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@",line)
                if not m:
                    return []
                left,right = map(int,m.groups())
            elif left is not None:
                left+=1
                right+=1
        elif left is not None and line.startswith(("-","+")):
            if line.startswith("-"):
                if line[1:].strip() and not line[1:].lstrip().startswith("#"):
                    before.append(left)
                left+=1
            else:
                if line[1:].strip() and not line[1:].lstrip().startswith("#"):
                    after.append(right)
                right+=1
    return groups


def scopes(tree):
    result={}
    for node in tree.body:
        if isinstance(node,ast.FunctionDef):
            result[node.name]=node
        elif isinstance(node,ast.ClassDef):
            for sub in node.body:
                if isinstance(sub,ast.FunctionDef):
                    result[node.name+"."+sub.name]=sub
                elif isinstance(sub,ast.Assign) and len(sub.targets)==1 and isinstance(sub.targets[0],ast.Name):
                    result[node.name+"."+sub.targets[0].id]=sub
    return result


def scope_for(nodes, lines):
    return next(((name,node) for name,node in nodes.items()
                 if node.lineno<=min(lines) and max(lines)<=node.end_lineno), (None,None))


def scope_groups(old,new,groups):
    for before,after in groups:
        pieces={}
        for side,(nodes,lines) in enumerate(((old,before),(new,after))):
            for line in lines:
                name,_=scope_for(nodes,[line])
                pieces.setdefault(name,([],[]))[side].append(line)
        for name,(left,right) in pieces.items():
            yield name,left,right


def syntax(node):
    return ast.dump(node,include_attributes=False)


def same_header(a,b):
    return (isinstance(a,ast.FunctionDef) and isinstance(b,ast.FunctionDef)
            and syntax(a.args)==syntax(b.args)
            and (syntax(a.returns) if a.returns else None)==(syntax(b.returns) if b.returns else None)
            and [syntax(v) for v in a.decorator_list]==[syntax(v) for v in b.decorator_list])


def deferred_registry_edit(a,b):
    """Existing keys with lambda bodies. Keep dictionary expansions and all other entries fixed."""
    if not (isinstance(a,ast.Assign) and isinstance(b,ast.Assign) and isinstance(a.value,ast.Dict)
            and isinstance(b.value,ast.Dict)):
        return False
    def entries(d):
        result={}
        for k,v in zip(d.keys,d.values):
            if k is not None and (not isinstance(k,ast.Constant) or type(k.value) is not str):
                return {}
            key=syntax(v) if k is None else k.value
            if key in result:
                return {}
            result[key]=v
        return result
    old,new=entries(a.value),entries(b.value)
    if not old or not new or not new.keys()<=old.keys() or list(new)!=[k for k in old if k in new]:
        return False
    changed=[k for k in old if k not in new or syntax(old[k])!=syntax(new[k])]
    return bool(changed) and all(isinstance(old[k],ast.Lambda) and
        (k not in new or isinstance(new[k],ast.Lambda) and syntax(old[k].args)==syntax(new[k].args))
        for k in changed)


def pure_guard(node,fn):
    if isinstance(node,ast.Constant):
        return True
    if isinstance(node,ast.Name):
        # The pinned _match returns a bool. An arbitrary local may call custom
        # Python code when an if statement coerces it to a truth value.
        writes=[v for v in ast.walk(fn) if isinstance(v,ast.Name) and
                v.id==node.id and isinstance(v.ctx,ast.Store)]
        return len(writes)==1 and any(
            isinstance(v,ast.Assign) and writes[0] in v.targets and isinstance(v.value,ast.Call)
            and isinstance(v.value.func,ast.Attribute) and v.value.func.attr=="_match"
            and isinstance(v.value.func.value,ast.Name) and v.value.func.value.id=="self"
            for v in ast.walk(fn))
    if isinstance(node,ast.BoolOp):
        return all(pure_guard(v,fn) for v in node.values)
    return (isinstance(node,ast.Attribute) and isinstance(node.value,ast.Name)
            and node.value.id=="self" and node.attr=="_prev_comments")


def removed_comment_argument(old,new,before,after):
    """On the pinned Parser, _match's expression parameter is used only for attaching comments."""
    def call(fn,lines):
        return next((n for n in ast.walk(fn) if isinstance(n,ast.Call) and n.lineno==n.end_lineno
                     and lines==[n.lineno] and isinstance(n.func,ast.Attribute)
                     and n.func.attr=="_match" and isinstance(n.func.value,ast.Name)
                     and n.func.value.id=="self"),None)
    a,b=call(old,before),call(new,after)
    if not a or not b or len(a.keywords)!=1 or a.keywords[0].arg!="expression" or b.keywords:
        return False
    v=a.keywords[0].value
    if not isinstance(v,ast.Name):
        if not (isinstance(v,ast.IfExp) and isinstance(v.body,ast.Name) and isinstance(v.orelse,ast.Constant)
                and v.orelse.value is None and isinstance(v.test,ast.Call)
                and ast.unparse(v.test) == f"isinstance({v.body.id}, exp.Expr)"):
            return False
    # Compare the entire function, so a second edit on the same line cannot be hidden.
    saved=a.keywords
    try:
        a.keywords=[]
        return syntax(old)==syntax(new)
    finally:
        a.keywords=saved


class TriggerGate:
    def __init__(self,inventory):
        tests=json.loads(inventory.read_text("utf8"))["tests"]
        data=json.loads(Path(__file__).with_name("top25_base.json").read_text("utf8"))
        self.events={t["number"]:{tuple(e) for e in data[t["id"]]} for t in
                     sorted(tests,key=lambda t:(-t["seconds"],t["number"]))[:25] if t["id"] in data}
        self.files={t["number"]:t["file"] for t in tests}

    def excluded_python(self,index,extra,path,old,new,chunk,candidates,aligned):
        eligible=self.events.keys() & candidates - index.base.uncertain
        if not eligible or not path.startswith("sqlglot/"):
            return set()
        if aligned and not (path=="sqlglot/parser.py" and re.search(r"(?m)^-.*self\._match\(.*expression=",chunk)):
            return set()
        eligible={n for n in eligible if n not in index.readers.get(path,()) or
                  extra.values("read:"+path,n)=={"import"}}
        if not eligible:
            return set()
        old_scopes,new_scopes=scopes(old),scopes(new)
        groups=edited_groups(chunk)
        if not groups:
            return set()
        exclusions=set(eligible)
        for name,before,after in scope_groups(old_scopes,new_scopes,groups):
            if name is None:
                return set()
            a,b=old_scopes.get(name),new_scopes.get(name)
            if not a:
                return set()
            cls,_,member=name.rpartition(".")
            plugin=path.startswith("sqlglot/parsers/") and cls.endswith("Parser")
            ordinary=(isinstance(a,ast.FunctionDef) and b and same_header(a,b) and not a.decorator_list and
                      ast.get_docstring(a)==ast.get_docstring(b) and
                      (not before or min(before)>=a.body[0].lineno) and
                      (not after or min(after)>=b.body[0].lineno))
            if plugin and (ordinary or
                           isinstance(a,ast.FunctionDef) and b is None and not a.decorator_list and
                           member.startswith("_parse_") or
                           member in {"FUNCTIONS","FUNCTION_PARSERS"} and deferred_registry_edit(a,b)):
                class_id=path[:-3].replace("/",".")+"."+cls
                # Include alternate uses of the registry that bypass a parser instance.
                keep=set(index.base.functions.get((path,name),()))
                keep.update(index.base.files.get("sqlglot/anonymize.py",()))
                exclusions.intersection_update(n for n in eligible if ("parser",class_id) not in self.events[n] and n not in keep)
            elif ordinary:
                matched=False
                if path=="sqlglot/parser.py":
                    if before and not after:
                        cond=next((v for v in ast.walk(a) if isinstance(v,ast.If) and not v.orelse
                                   and v.lineno==min(before) and v.end_lineno==max(before)
                                   and pure_guard(v.test,a)),None)
                        if cond:
                            mask=index.mask(index.data["lines"].get(path,{}),str(cond.body[0].lineno))
                            if mask:
                                exclusions.difference_update(index.hits(mask))
                                matched=True
                    elif removed_comment_argument(a,b,before,after):
                        exclusions.intersection_update(n for n in eligible
                            if ("comment",path,before[0]) not in self.events[n])
                        matched=True
                if not matched:
                    hits=index.base.functions.get((path,name))
                    if not hits:
                        return set()
                    exclusions.difference_update(hits)
            else:
                return set()
            if not exclusions:
                break
        return exclusions

    def excluded_fixture(self,index,chunk,candidates):
        # On the pinned base these particular files are read via the pair loader in
        # TestExecutor.setUpClass. Other fixtures may have additional raw-file consumers.
        header=re.search(r"(?m)^--- a/(tests/fixtures/optimizer/(tpc-[hd]s?)/\2\.sql)$",chunk)
        if not header or not re.search(r"(?m)^\+\+\+ b/"+re.escape(header[1])+r"$",chunk):
            return set()
        path=header[1]
        eligible={n for n in self.events.keys() & candidates - index.base.uncertain
                  if self.files[n]=="tests/test_executor.py"}
        if not eligible:
            return set()
        try:
            base=(index.source/path).read_text("utf8")
            changed=apply_file(base,chunk)[0]
        except (ValueError,OSError):
            return set()
        def statements(s):
            return "\n".join(line for line in s.splitlines() if line and not line.startswith("--")).split(";")
        left,right=statements(base),statements(changed)
        if len(left)!=len(right) or left[-1].strip() or right[-1].strip():
            return set()
        slots=set()
        for i,(a,b) in enumerate(zip(left,right)):
            if a.strip()!=b.strip():
                if i%2==0:  # Preserve metadata while checking the raw input slot.
                    if [l for l in a.splitlines() if l.startswith("#")] != [l for l in b.splitlines() if l.startswith("#")]:
                        return set()
                slots.add(1+i%2)
        key=path.removeprefix("tests/fixtures/")
        return {n for n in eligible if ("pair",key,-1) in self.events[n]
                and all(("pair",key,i) not in self.events[n] for i in slots)}
