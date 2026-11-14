import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const repository = fileURLToPath(new URL("../../", import.meta.url));
const source = fileURLToPath(new URL("../src/comments/types.ts", import.meta.url));

function webVariants() {
  const program = ts.createProgram([source], { strictNullChecks: true });
  const checker = program.getTypeChecker();
  const module = program.getSourceFile(source);
  const declaration = module.statements.find(
    (statement) => ts.isTypeAliasDeclaration(statement) && statement.name.text === "CommentAnchor",
  );
  assert.ok(declaration, "CommentAnchor must be exported by the shared module");
  const anchor = checker.getTypeAtLocation(declaration);
  assert.ok(anchor.isUnion(), "CommentAnchor must discriminate the anchor variants");
  return Object.fromEntries(
    anchor.types.map((variant) => {
      const fields = checker.getPropertiesOfType(variant);
      const kind = fields.find((field) => field.name === "kind");
      const literal = checker.getTypeOfSymbolAtLocation(kind, declaration);
      const nullable = fields
        .filter((field) => {
          const value = checker.getTypeOfSymbolAtLocation(field, declaration);
          return (value.isUnion() ? value.types : [value]).some(
            (member) => member.flags & ts.TypeFlags.Null,
          );
        })
        .map((field) => field.name)
        .sort();
      return [literal.value, { fields: fields.map((field) => field.name).sort(), nullable }];
    }),
  );
}

function pythonVariants(schema) {
  const resolve = (value) => (value.$ref ? schema.$defs[value.$ref.split("/").at(-1)] : value);
  return Object.fromEntries(
    (schema.oneOf ?? schema.anyOf).map((item) => {
      const variant = resolve(item);
      const fields = Object.keys(variant.properties).sort();
      const nullable = fields.filter((name) => {
        const property = resolve(variant.properties[name]);
        return (property.anyOf ?? [property]).some((member) => resolve(member).type === "null");
      });
      return [variant.properties.kind.const, { fields, nullable }];
    }),
  );
}

test("shared CommentAnchor fields and nullability match the Python contract", (context) => {
  const result = spawnSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `
import importlib.util
import json
import sys
if importlib.util.find_spec("rcp.comments") is None:
    sys.exit(77)
from pydantic import TypeAdapter
from rcp.comments.models import CommentAnchor
print(json.dumps(TypeAdapter(CommentAnchor).json_schema()))
`,
    ],
    { cwd: repository, encoding: "utf8", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" } },
  );
  if (result.status === 77) {
    context.skip("The parallel backend slice has not supplied rcp.comments yet");
    return;
  }
  assert.equal(result.status, 0, result.error?.message ?? result.stderr);
  assert.deepEqual(webVariants(), pythonVariants(JSON.parse(result.stdout)));
});
