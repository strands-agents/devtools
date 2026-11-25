/**
 * NoTokenState — the ONE "add a GitHub token" empty state every GitHub-backed page shows in mock /
 * no-token mode (Today, Issues, PRs and their detail pages). Same icon, copy shape, and 40px
 * "Open Settings" button everywhere; the page supplies what the token would unlock.
 */
import { Button, Stack, Text } from "@mantine/core";
import { IconBrandGithub } from "@tabler/icons-react";
import { Link } from "react-router-dom";
import { ROUTES } from "../lib/links";

export function NoTokenState({ title, message }: { title?: string; message: string }) {
  return (
    <Stack align="center" gap="sm" py="xl">
      <IconBrandGithub size={32} stroke={1.5} />
      {title ? <Text fw={600}>{title}</Text> : null}
      <Text c="dimmed" size="sm" ta="center" maw={420}>
        {message}
      </Text>
      <Button component={Link} to={ROUTES.settings} variant="light" h={40} mt="xs">
        Add a GitHub token in Settings
      </Button>
    </Stack>
  );
}
