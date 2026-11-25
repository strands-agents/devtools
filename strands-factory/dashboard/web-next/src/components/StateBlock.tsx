import { Alert, Center, Loader, Stack, Text } from "@mantine/core";
import { IconAlertTriangle } from "@tabler/icons-react";

export function LoadingBlock({ label = "Loading…" }: { label?: string }) {
  return (
    <Center py="xl">
      <Stack align="center" gap="xs">
        <Loader />
        <Text c="dimmed" size="sm">
          {label}
        </Text>
      </Stack>
    </Center>
  );
}

export function ErrorBlock({ error }: { error: string }) {
  return (
    <Alert color="red" icon={<IconAlertTriangle size={18} />} title="Something went wrong" my="md">
      {error}
    </Alert>
  );
}

export function EmptyBlock({ label }: { label: string }) {
  return (
    <Center py="xl">
      <Text c="dimmed">{label}</Text>
    </Center>
  );
}
