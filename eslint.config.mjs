import fioriTools from '@sap-ux/eslint-plugin-fiori-tools';

export default [
    {
        ignores: ["dist/**", ".venv/**"]
    },
    ...fioriTools.configs.recommended,
    {
        languageOptions: {
            ecmaVersion: 2020,
            globals: {
                Promise: "readonly"
            }
        },
        rules: {
            "@sap-ux/fiori-tools/sap-timeout-usage": "off",
            camelcase: ["warn", {
                ignoreDestructuring: true,
                properties: "never"
            }],
            "linebreak-style": "off",
            "no-use-before-define": ["warn", {
                classes: true,
                functions: false,
                variables: true
            }]
        }
    },
    {
        files: ["webapp/model/presentation/experimentEvidenceHtml.js"],
        rules: {
            complexity: "off"
        }
    }
];
