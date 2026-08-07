import threading

from remi.gui import Button, CheckBox, Container, DropDown, Label, TextInput


def apply_common_style(widget, left, top, width, height, position="absolute", percent=False):
    widget.css_position = position
    widget.css_left = f"{left}px"
    widget.css_top = f"{top}px"
    if percent:
        widget.css_width = f"{width}%"
        widget.css_height = f"{height}%"
    else:
        widget.css_width = f"{width}px"
        widget.css_height = f"{height}px"


class StyledContainer(Container):
    def __init__(self, variable_name, left, top, width=650, height=650, border=False, bg_color=False,
                 color="#707070", position="absolute", percent=False, overflow=False, container=None,
                 line="1.5px solid #888"):
        super().__init__()
        apply_common_style(self, left, top, width, height, position, percent)
        self.variable_name = variable_name
        if border:
            self.style["border"] = line
            self.style["border-radius"] = "4px"
        if bg_color:
            self.style["background-color"] = color
        if overflow:
            self.style.update({
                "overflow": "auto",
                "overflow-y": "scroll",
                "overflow-x": "hidden",
                "max-height": "320px",
                "scrollbar-width": "thin",
                "border-radius": "4px",
                "padding-right": "4px",
            })
        if container:
            container.append(self, self.variable_name)


class StyledButton(Button):
    def __init__(self, text, variable_name, left, top,
                 normal_color="#007BFF", press_color="#0056B3",
                 width=100, height=30, font_size=90,
                 position="absolute", percent=False, container=None):
        super().__init__(text)
        apply_common_style(self, left, top, width, height, position, percent)

        self.variable_name = variable_name
        self.normal_color = normal_color
        self.press_color = press_color
        self.style.update({
            "background-color": normal_color,
            "color": "white",
            "border": "none",
            "border-radius": "4px",
            "box-shadow": "0 2px 5px rgba(0,0,0,0.2)",
            "cursor": "pointer",
            "font-size": f"{font_size}%",
        })

        self.onmousedown.do(lambda w, *a: w.style.update({"background-color": self.press_color}))

        def _recover_and_call(w, *a):
            w.style.update({"background-color": self.normal_color})
            if hasattr(self, "_user_callback"):
                threading.Thread(target=self._user_callback, args=(w, *a), daemon=True).start()

        self.onmouseup.do(_recover_and_call)
        self.onmouseleave.do(lambda w, *a: w.style.update({"background-color": self.normal_color}))

        if container:
            container.append(self, variable_name)

    def do_onclick(self, cb):
        self._user_callback = lambda *_: cb()


class StyledLabel(Label):
    def __init__(self, text, variable_name, left, top,
                 width=150, height=20, font_size=100, color="#444", align="left",
                 position="absolute", percent=False, bold=False, flex=False,
                 justify_content="center", on_line=False, border=False, container=None):
        super().__init__(text)
        apply_common_style(self, left, top, width, height, position, percent)
        self.css_font_size = f"{font_size}%"
        self.variable_name = variable_name
        if flex:
            self.style.update({
                "display": "flex",
                "justify-content": justify_content,
                "align-items": "center",
            })
        else:
            self.css_text_align = align
        self.style["color"] = color
        if bold:
            self.style["font-weight"] = "bold"
        if on_line:
            self.style["background-color"] = "white"
        if border:
            self.style["border"] = "1.5px solid #888"
            self.style["border-radius"] = "4px"
        if container:
            container.append(self, self.variable_name)


class StyledDropDown(DropDown):
    def __init__(self, text, variable_name, left, top,
                 width=220, height=30, font_size=100, bg_color="#f9f9f9",
                 border="1px solid #aaa", border_radius="4px", padding="3px",
                 position="absolute", percent=False, container=None):
        super().__init__()
        self.append(text)
        apply_common_style(self, left, top, width, height, position, percent)
        self.css_font_size = f"{font_size}%"
        self.variable_name = variable_name
        self.style.update({
            "background-color": bg_color,
            "border": border,
            "border-radius": border_radius,
            "padding": padding,
        })
        if container:
            container.append(self, self.variable_name)


class StyledCheckBox(CheckBox):
    def __init__(self, variable_name, left, top, width=30, height=30,
                 position="absolute", percent=False, container=None):
        super().__init__()
        apply_common_style(self, left, top, width, height, position, percent)
        self.css_margin = "5px"
        self.variable_name = variable_name
        if container:
            container.append(self, self.variable_name)


class StyledTextInput(TextInput):
    def __init__(self, variable_name, left, top, width=150, height=30, text="",
                 position="absolute", percent=False, container=None):
        super().__init__()
        apply_common_style(self, left, top, width, height, position, percent)
        self.set_text(text)
        self.variable_name = variable_name
        self.style.update({
            "padding": "0 8px",
            "border": "1px solid #aaa",
            "border-radius": "4px",
            "box-shadow": "inset 0 1px 3px rgba(0,0,0,0.1)",
            "background-color": "#fafafa",
            "font-size": "15px",
            "color": "#333",
            "line-height": f"{height}px",
            "overflow": "hidden",
            "text-align": "center",
            "display": "flex",
            "align-items": "center",
            "justify-content": "center",
            "white-space": "nowrap",
            "overflow-x": "hidden",
            "overflow-y": "hidden",
            "resize": "none",
        })
        if container:
            container.append(self, self.variable_name)
