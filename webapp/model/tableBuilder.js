sap.ui.define([
    "sap/m/Table",
    "sap/m/Column",
    "sap/m/ColumnListItem",
    "sap/m/Text"
], (Table, Column, ColumnListItem, Text) => {
    "use strict";

    function createSensorPlacementTable() {
        const table = new Table({
            growing: true,
            growingThreshold: 20,
            items: {
                path: "/sensorPlacements",
                template: new ColumnListItem({
                    cells: [
                        new Text({ text: "{rank}" }),
                        new Text({ text: "{pot_code}" }),
                        new Text({ text: "{pot_label}" }),
                        new Text({ text: "{balcony_zone}" }),
                        new Text({ text: "{sun_exposure}" }),
                        new Text({ text: "{size_class}" }),
                        new Text({ text: "{plant_type_label}" }),
                        new Text({ text: "{score}" }),
                        new Text({ text: "{reason}" })
                    ]
                })
            }
        });

        [
            ["Rank", "4rem"],
            ["Code", "6rem"],
            ["Pot", "12rem"],
            ["Zone", "9rem"],
            ["Sun", "8rem"],
            ["Size", "7rem"],
            ["Plant", "9rem"],
            ["Score", "5rem"],
            ["Reason", "18rem"]
        ].forEach(([label, width]) => {
            table.addColumn(new Column({ width, header: new Text({ text: label }) }));
        });

        return table;
    }

    return {
        createSensorPlacementTable
    };
});
