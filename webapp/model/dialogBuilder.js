sap.ui.define([
    "sap/m/Dialog",
    "sap/m/Button",
    "disertatie/model/tableBuilder"
], (Dialog, Button, TableBuilder) => {
    "use strict";

    function getSensorPlacementDialog(controller) {
        if (controller._sensorPlacementDialog) {
            return controller._sensorPlacementDialog;
        }

        controller._sensorPlacementDialog = new Dialog({
            title: "Sensor locations",
            contentWidth: "72rem",
            contentHeight: "30rem",
            resizable: true,
            draggable: true,
            content: [TableBuilder.createSensorPlacementTable()],
            endButton: new Button({
                text: "Close",
                press: () => controller._sensorPlacementDialog.close()
            })
        });
        controller.getView().addDependent(controller._sensorPlacementDialog);
        return controller._sensorPlacementDialog;
    }

    function openSensorPlacementDialog(controller) {
        getSensorPlacementDialog(controller).open();
    }

    return {
        getSensorPlacementDialog,
        openSensorPlacementDialog
    };
});
