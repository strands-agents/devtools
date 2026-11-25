/**
 * The chat composer's model field: any harness model string and effort, typed or picked from a few
 * suggestions. Empty runs the deployment's default.
 */
import { Autocomplete, Group, Text, Tooltip } from "@mantine/core";
import type { SessionRun } from "../api/types";
import { EFFORT_SUGGESTIONS, MODEL_SUGGESTIONS, modelLabel, type ModelSelection } from "../lib/modelChoice";

export function ModelPicker({
  selection,
  onChange,
  disabled,
}: {
  selection: ModelSelection;
  onChange: (next: ModelSelection) => void;
  disabled?: boolean;
}) {
  return (
    <Group gap={6} wrap="nowrap">
      <Autocomplete
        aria-label="Model"
        size="xs"
        w={280}
        placeholder="Default model"
        data={MODEL_SUGGESTIONS}
        value={selection.model}
        onChange={(model) => onChange({ ...selection, model })}
        clearable
        disabled={disabled}
        comboboxProps={{ withinPortal: true }}
      />
      <Autocomplete
        aria-label="Effort"
        size="xs"
        w={120}
        placeholder="Default effort"
        data={EFFORT_SUGGESTIONS}
        value={selection.effort}
        onChange={(effort) => onChange({ ...selection, effort })}
        clearable
        disabled={disabled}
        comboboxProps={{ withinPortal: true }}
      />
    </Group>
  );
}

/** The model of the session's newest run, with every run's in its tooltip. Nothing without one. */
export function RunModels({ runs }: { runs: SessionRun[] }) {
  const recorded = runs.filter((r) => r.model);
  const last = recorded[recorded.length - 1];
  if (!last) return null;
  const describe = (r: SessionRun) => `${modelLabel(r.model)}${r.effort ? ` · ${r.effort}` : ""}`;
  const lines = recorded.map((r) => `${r.startedAt ? new Date(r.startedAt).toLocaleString() + ": " : ""}${describe(r)}`);
  return (
    <Tooltip label={lines.join("\n")} multiline maw={360} style={{ whiteSpace: "pre-line" }} withArrow>
      <Text size="xs" c="dimmed" truncate aria-label={`Last run: ${describe(last)}`} tabIndex={0}>
        Last run: {describe(last)}
      </Text>
    </Tooltip>
  );
}
