import LiveTranscriptCore
import SwiftUI

struct ContentView: View {
    @ObservedObject var model: AppModel
    @State private var confirmClear = false

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(spacing: 16) {
                    connectionCard
                    statusCard
                    captureCard
                    finishingCard
                    transcriptCard
                }
                .padding(16)
            }
            .background(Color(uiColor: .systemGroupedBackground))
            .navigationTitle("Live Transcript")
            .alert(
                "無法完成操作",
                isPresented: Binding(
                    get: { model.errorMessage != nil },
                    set: { if !$0 { model.errorMessage = nil } }
                )
            ) {
                Button("知道了", role: .cancel) { model.errorMessage = nil }
            } message: {
                Text(model.errorMessage ?? "")
            }
            .confirmationDialog(
                "要清除畫面上的逐字稿嗎？已保存檔案不受影響。",
                isPresented: $confirmClear,
                titleVisibility: .visible
            ) {
                Button("清除畫面", role: .destructive) { model.clearTranscript() }
            }
        }
    }

    private var connectionCard: some View {
        GroupBox("Mac 伺服器") {
            VStack(alignment: .leading, spacing: 12) {
                TextField("https://your-server.example", text: $model.serverURLString)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    .textContentType(.URL)
                    .autocorrectionDisabled()
                    .textFieldStyle(.roundedBorder)
                    .disabled(model.isCapturing || model.isStarting)
                    .onSubmit(model.connect)
                Button {
                    model.connect()
                } label: {
                    Label(
                        model.isConnecting ? "連線中…" : "連線",
                        systemImage: "network"
                    )
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.isConnecting || model.isCapturing || model.serverURLString.isEmpty)
            }
            .padding(.top, 8)
        }
    }

    private var statusCard: some View {
        GroupBox("系統狀態") {
            VStack(spacing: 12) {
                StatusRow(
                    title: "服務",
                    message: model.serviceMessage,
                    state: model.serviceState
                )
                StatusRow(
                    title: "模型",
                    message: model.modelMessage,
                    state: model.modelState
                )
                Button(model.modelState == "loaded" ? "卸載模型" : "啟用模型") {
                    model.toggleModel()
                }
                .buttonStyle(.bordered)
                .frame(maxWidth: .infinity, alignment: .trailing)
                .disabled(!model.canToggleModel)
            }
            .padding(.top, 8)
        }
    }

    private var captureCard: some View {
        GroupBox("錄音設定") {
            VStack(alignment: .leading, spacing: 14) {
                Picker("音訊來源", selection: $model.captureMode) {
                    ForEach(CaptureMode.allCases) { mode in
                        Text(mode.rawValue).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .disabled(model.isCapturing || model.isStarting)

                if model.captureMode == .screen {
                    Label(
                        "開始後請在系統分享選擇器中決定是否同時開啟麥克風。",
                        systemImage: "rectangle.on.rectangle"
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }

                Picker("辨識語言", selection: $model.language) {
                    ForEach(RecognitionLanguage.allCases, id: \.self) { language in
                        Text(language.displayName).tag(language)
                    }
                }
                .disabled(model.isCapturing || model.isStarting)

                TextField("逐字稿名稱（可留空）", text: $model.sessionName)
                    .textFieldStyle(.roundedBorder)
                    .disabled(model.isCapturing || model.isStarting)

                HStack {
                    Button {
                        model.startRecording()
                    } label: {
                        Label(
                            model.isStarting ? "準備中…" : "開始錄音",
                            systemImage: "record.circle"
                        )
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!model.canStart)

                    Button(role: .destructive) {
                        model.stopRecording()
                    } label: {
                        Label("停止", systemImage: "stop.fill")
                    }
                    .buttonStyle(.bordered)
                    .disabled(!model.isCapturing || model.isStopping)
                }
            }
            .padding(.top, 8)
        }
    }

    @ViewBuilder
    private var finishingCard: some View {
        if model.isFinishing {
            HStack(spacing: 12) {
                ProgressView()
                VStack(alignment: .leading, spacing: 3) {
                    Text("正在完成剩餘辨識")
                        .font(.headline)
                    Text("完成保存前無法開始下一場錄音或卸載模型。")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
            .background(.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
        }
    }

    private var transcriptCard: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Text("即時逐字稿")
                        .font(.title3.bold())
                    Spacer()
                    if !model.transcripts.isEmpty {
                        Button("清除") { confirmClear = true }
                            .font(.subheadline)
                    }
                }

                if model.transcripts.isEmpty {
                    ContentUnavailableView(
                        "等待語音",
                        systemImage: "waveform",
                        description: Text("辨識完成的內容會出現在這裡。")
                    )
                    .frame(minHeight: 180)
                } else {
                    ForEach(model.transcripts) { item in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(item.speaker).font(.caption.bold())
                                Spacer()
                                Text(item.timestamp)
                                    .font(.caption.monospacedDigit())
                                    .foregroundStyle(.secondary)
                            }
                            Text(item.text)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .padding(.vertical, 6)
                        Divider()
                    }
                }

                if let markdownURL = model.markdownURL,
                   let jsonlURL = model.jsonlURL {
                    HStack {
                        Link(destination: markdownURL) {
                            Label("Markdown", systemImage: "arrow.down.doc")
                        }
                        Spacer()
                        Link(destination: jsonlURL) {
                            Label("JSONL", systemImage: "arrow.down.doc")
                        }
                    }
                    .buttonStyle(.bordered)
                }
            }
            .padding(.top, 4)
        }
    }
}

private struct StatusRow: View {
    let title: String
    let message: String
    let state: String

    private var color: Color {
        switch state {
        case "loaded", "recording", "waiting": .green
        case "loading", "unloading", "processing", "connecting", "starting": .orange
        case "error": .red
        default: .gray
        }
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Circle().fill(color).frame(width: 9, height: 9)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.caption.bold()).foregroundStyle(.secondary)
                Text(message).font(.subheadline)
            }
            Spacer()
            if ["loading", "unloading", "processing", "connecting", "starting"].contains(state) {
                ProgressView().controlSize(.small)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
