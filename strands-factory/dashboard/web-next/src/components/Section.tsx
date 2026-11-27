/** A titled settings/detail block: heading + one-line description + content, on a raised surface. */
import type { ReactNode } from "react";
import { Group, Paper, Stack, Text } from "@mantine/core";

export function Section({
  title,
  description,
  icon,
  children,
  actions,
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
  children: ReactNode;
  /** Right-aligned header actions. */
  actions?: ReactNode;
}) {
  return (
    <Paper p="md">
      <Stack gap="sm">
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <div>
            <Group gap={8}>
              {icon}
              <Text fw={650}>{title}</Text>
            </Group>
            {description ? (
              <Text size="sm" c="dimmed" mt={2}>
                {description}
              </Text>
            ) : null}
          </div>
          {actions}
        </Group>
        {children}
      </Stack>
    </Paper>
  );
}
