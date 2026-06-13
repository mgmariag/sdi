sap.ui.define([], () => {
    "use strict";

    const PROFILE = Object.freeze({
        id: "MG",
        email: "",
        firstName: "M",
        lastName: "G",
        fullName: "M G"
    });

    let installed = false;

    function overrideMethod(target, name, value) {
        if (target && typeof target[name] === "function") {
            target[name] = () => value;
        }
    }

    // The patching functions below are designed to override the default user information 
    // provided by the SAP Fiori Launchpad (FLP) with a custom profile.
    // The functions ensure that any component or service that retrieves user information 
    // will receive the custom profile data instead of the default "Default User" or "DU".
    // Additionally, the code includes a mechanism to replace any generated text nodes in 
    // the DOM that reference the default user with the custom profile information, 
    // ensuring a consistent user experience across the application. 
    function patchUserInfo(userInfo) {
        if (!userInfo) {
            return;
        }
        Object.assign(userInfo, PROFILE);
        overrideMethod(userInfo, "getId", PROFILE.id);
        overrideMethod(userInfo, "getEmail", PROFILE.email);
        overrideMethod(userInfo, "getFirstName", PROFILE.firstName);
        overrideMethod(userInfo, "getLastName", PROFILE.lastName);
        overrideMethod(userInfo, "getFullName", PROFILE.fullName);
    }

    function patchContainer(container) {
        if (!container) {
            return;
        }
        if (typeof container.getUser === "function") {
            patchUserInfo(container.getUser());
        }
        if (typeof container.getServiceAsync === "function") {
            container.getServiceAsync("UserInfo").then(patchUserInfo).catch(() => undefined);
        }
    }

    function replaceGeneratedTextNode(textNode) {
        if (!textNode || !textNode.nodeValue) {
            return;
        }
        if (textNode.nodeValue.trim() === "DU") {
            textNode.nodeValue = textNode.nodeValue.replace("DU", PROFILE.id);
            return;
        }
        if (textNode.nodeValue.includes("Default User")) {
            textNode.nodeValue = textNode.nodeValue.replace(/Default User/g, PROFILE.fullName);
        }
    }

    function replaceGeneratedText(root) {
        if (!root) {
            return;
        }
        if (root.nodeType === 3) {
            replaceGeneratedTextNode(root);
            return;
        }
        if (typeof document.createTreeWalker !== "function") {
            return;
        }
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        const textNodes = [];
        let node = walker.nextNode();
        while (node) {
            if (
                node.nodeValue &&
                (node.nodeValue.includes("Default User") || node.nodeValue.trim() === "DU")
            ) {
                textNodes.push(node);
            }
            node = walker.nextNode();
        }
        textNodes.forEach(replaceGeneratedTextNode);
        document.querySelectorAll(
            "[title='Default User'],[aria-label='Default User'],[title='DU'],[aria-label='DU']"
        ).forEach((element) => {
            if (element.getAttribute("title") === "Default User") {
                element.setAttribute("title", PROFILE.fullName);
            }
            if (element.getAttribute("title") === "DU") {
                element.setAttribute("title", PROFILE.id);
            }
            if (element.getAttribute("aria-label") === "Default User") {
                element.setAttribute("aria-label", PROFILE.fullName);
            }
            if (element.getAttribute("aria-label") === "DU") {
                element.setAttribute("aria-label", PROFILE.id);
            }
        });
    }

    function installGeneratedTextFallback() {
        replaceGeneratedText(document.body);
        if (!document.body || typeof MutationObserver !== "function") {
            return;
        }
        const observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => replaceGeneratedText(mutation.target));
        });
        observer.observe(document.body, {
            attributes: true,
            childList: true,
            characterData: true,
            subtree: true
        });
    }

    function install() {
        if (installed) {
            return;
        }
        installed = true;
        patchContainer(sap.ushell && sap.ushell.Container);
        sap.ui.require(["sap/ushell/Container"], patchContainer, () => {
            patchContainer(sap.ushell && sap.ushell.Container);
        });

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", installGeneratedTextFallback, { once: true });
        } else {
            installGeneratedTextFallback();
        }
    }

    install();

    return {
        install
    };
});
