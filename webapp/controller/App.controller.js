sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/m/MessageBox"
], (BaseController, MessageBox) => {
  "use strict";

  return BaseController.extend("disertatie.controller.App", {
      onInit() {
          MessageBox.alert("Welcome to the app!");
      }
  });
});
