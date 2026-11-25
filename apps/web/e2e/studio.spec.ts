import { expect, test } from "@playwright/test";

test.describe("FlowPilot Visual Studio E2E", () => {
  test("renders starter canvas, templates gallery, approval inbox, auth, and AI draft modals", async ({
    page,
  }) => {
    await page.goto("/");

    await expect(page.getByText("FlowPilot Studio")).toBeVisible();
    await expect(page.getByText("Node Library")).toBeVisible();
    await expect(page.getByText("Order Intake Trigger")).toBeVisible();
    await expect(page.getByText("Finance Approval Gate")).toBeVisible();

    // Templates Gallery
    await page.getByRole("button", { name: /Templates/i }).click();
    await expect(page.getByText("Curated Workflow Templates Gallery")).toBeVisible();
    await page.getByRole("button", { name: /Clone to Canvas/i }).first().click();

    // Approval Inbox Modal
    await page.getByRole("button", { name: /Inbox/i }).click();
    await expect(page.getByText(/Human Approvals Inbox/i)).toBeVisible();
    await page.locator(".modal-card button").first().click();

    // AI Draft Modal
    await page.getByRole("button", { name: /AI Draft/i }).click();
    await expect(
      page.getByText("Generate Workflow Draft from Natural Language"),
    ).toBeVisible();
    await page.getByRole("button", { name: "Cancel" }).click();

    // Inspector tabs
    await page.getByRole("button", { name: /Validate/i }).click();
    await expect(page.getByText("Pre-Publish Diagnostics")).toBeVisible();

    await page.getByRole("button", { name: /Vault/i }).click();
    await expect(page.getByText(/Envelope-Encrypted Vault/i)).toBeVisible();
  });
});
