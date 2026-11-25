import { expect, test } from "@playwright/test";

test.describe("FlowPilot Visual Studio", () => {
  test("renders starter workflow canvas, node library, inspector, and modals", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByText("FlowPilot Studio")).toBeVisible();
    await expect(page.getByText("Node Library")).toBeVisible();
    await expect(page.getByText("Order Intake Trigger")).toBeVisible();
    await expect(page.getByText("Finance Approval Gate")).toBeVisible();

    // Open AI Draft modal
    await page.getByRole("button", { name: /AI Draft/i }).click();
    await expect(
      page.getByText("Generate Workflow Draft from Natural Language"),
    ).toBeVisible();
    await page.getByRole("button", { name: "Cancel" }).click();

    // Switch Inspector tabs
    await page.getByRole("button", { name: /Validate/i }).click();
    await expect(page.getByText("Pre-Publish Diagnostics")).toBeVisible();

    await page.getByRole("button", { name: /Vault/i }).click();
    await expect(page.getByText(/Envelope-Encrypted Vault/i)).toBeVisible();
  });
});
