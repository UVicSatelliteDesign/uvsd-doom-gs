from PyQt6.QtWidgets import QComboBox, QLabel, QPushButton, QVBoxLayout, QWidget

from messages import (
    CMD_ACK,
    CMD_ACTIVATE_BURN_WIRE,
    CMD_DOOM_EOF,
    CMD_DOOM_GAMEPAD,
    CMD_DOOM_KEYBOARD,
    CMD_FRAME_RECEIVED,
    CMD_GAME_STATE_RECEIVED,
    CMD_NACK_GAME_STATE_DATA,
    CMD_NACK_IMAGE_DATA,
    CMD_PING,
    CMD_PLAY_DOOM,
    CMD_REQUEST_GAME_STATE,
    CMD_REQUEST_TELEMETRY,
    CMD_RESET_DOOM,
    CMD_RESET_DOOM_BUFFER,
)


class CommandPage (QWidget):
    def __init__(self):
        super().__init__()

        command_label = QLabel("Select Command:")

        self.command_box = QComboBox()

        self.send_button = QPushButton("Send Command")
        self.send_button.clicked.connect(self.send_command)

        # Populate the dropdown with available uplink commands
        self.command_box.addItem("Ping", CMD_PING)
        self.command_box.addItem("DOOM Command: Keyboard", CMD_DOOM_KEYBOARD)
        self.command_box.addItem("DOOM Command: Gamepad", CMD_DOOM_GAMEPAD)
        self.command_box.addItem("DOOM Command EOF", CMD_DOOM_EOF)
        self.command_box.addItem("Request Telemetry", CMD_REQUEST_TELEMETRY)
        self.command_box.addItem("Activate Burn Wire", CMD_ACTIVATE_BURN_WIRE)
        self.command_box.addItem("Play DOOM", CMD_PLAY_DOOM)
        self.command_box.addItem("Reset DOOM Buffer", CMD_RESET_DOOM_BUFFER)
        self.command_box.addItem("Reset DOOM", CMD_RESET_DOOM)
        self.command_box.addItem("ACK", CMD_ACK)
        self.command_box.addItem("NACK Image Data", CMD_NACK_IMAGE_DATA)
        self.command_box.addItem("Frame received", CMD_FRAME_RECEIVED)
        self.command_box.addItem("Request Game State", CMD_REQUEST_GAME_STATE)
        self.command_box.addItem("NACK Game state data", CMD_NACK_GAME_STATE_DATA)
        self.command_box.addItem("Game state received", CMD_GAME_STATE_RECEIVED)

        layout = QVBoxLayout()
        layout.addWidget(command_label)
        layout.addWidget(self.command_box)
        layout.addWidget(self.send_button)
        layout.addStretch()

        self.setLayout(layout)

    def send_command(self):
            command = self.command_box.currentData()

            # Placeholder until command transmission implemented
            print(f"Selected command: 0x{command:02X}")