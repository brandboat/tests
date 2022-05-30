import { SupportPage } from "@/pageobjects/support.po";

describe("Support Page", () => {
  const page = new SupportPage();
  before(() => cy.task("deleteFolder", Cypress.config("downloadsFolder")))

  beforeEach(() => {
    page.visit();
  })

  context("Links in Community Support Section", () => {
    it("Should Link to correct URLs", () => {
      page.docsBtn.invoke("attr", "href")
          .should("eq", "https://docs.harvesterhci.io/latest/")

      page.forumsBtn.invoke("attr", "href")
          .should("eq", "https://forums.rancher.com/c/harvester/")

      page.slackBtn.invoke("attr", "href")
          .should("eq", "https://slack.rancher.io")

      page.fileAnIssueBtn.invoke("attr", "href")
          .should("eq", "https://github.com/harvester/harvester/issues/new/choose")
    })
  })

  context("Download KubeConfig File", () => {
    it("Should be Downloaded", () => {
      const kubeconfig = `${Cypress.config("downloadsFolder")}/local.yaml`
      page.downloadKubeConfigBtn.click()

      cy.readFile(kubeconfig)
        .should("exist")

      cy.task("readYaml", kubeconfig)
        .should(val => expect(val).to.not.be.a('string'))

    })
  })

  context('Generate Support Bundle', () => {
    it('is required to input Description', () => {
      page.generateSupportBundleBtn.click()

      page.inputSupportBundle()
          .get("@generateBtn").click()

      // to verify the button renamed with `Error`
      cy.get("@generateBtn")
        .should("be.not.disabled")
        .should("contain", "Error")

      // to verify the error message displayed
      cy.get("@generateView")
        .get(".banner.error")
        .should("be.visible")

      // to verify the view disappeared.
      cy.get("@closeBtn").click()
      cy.get("@generateView")
        .children()
        .should($el => expect($el).to.have.length(0))
    })
  });
})
