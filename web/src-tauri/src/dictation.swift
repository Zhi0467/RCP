import AVFoundation
import Foundation
import Speech

public typealias DictationCallback = @convention(c) (
    UnsafePointer<CChar>, UnsafePointer<CChar>, UnsafePointer<CChar>, Int32,
    UnsafePointer<CChar>, UnsafePointer<CChar>
) -> Void

// The Objective-C controller calls these entry points only on the main queue.
@_cdecl("rcp_analyzer_start")
@MainActor
public func startAnalyzer(_ session: UnsafePointer<CChar>, _ callback: DictationCallback) -> Int32 {
    if #available(macOS 26, *) {
        let session = AnalyzerSession(id: String(cString: session), callback: callback)
        AnalyzerSession.current = session
        session.setupTask = Task { await session.start() }
        return 1
    }
    return 0
}

@_cdecl("rcp_analyzer_stop")
@MainActor
public func stopAnalyzer(_ finish: Int32) {
    if #available(macOS 26, *) {
        AnalyzerSession.current?.stop(finish: finish != 0)
    }
}

@available(macOS 26, *)
@MainActor
private final class AnalyzerSession {
    static var current: AnalyzerSession?
    let id: String
    let callback: DictationCallback
    var active = true
    var finishing = false
    var setupTask: Task<Void, Never>?
    var resultsTask: Task<Void, Never>?
    var analyzer: SpeechAnalyzer?
    var engine: AVAudioEngine?
    var input: AsyncStream<AnalyzerInput>.Continuation?
    var finalized = ""
    var volatile = ""

    init(id: String, callback: @escaping DictationCallback) {
        self.id = id
        self.callback = callback
    }

    func emit(_ kind: String, text: String = "", final: Bool = false,
              state: String = "", error: String = "") {
        guard active else { return }
        id.withCString { id in
            kind.withCString { kind in
                text.withCString { text in
                    state.withCString { state in
                        error.withCString { error in
                            callback(id, kind, text, final ? 1 : 0, state, error)
                        }
                    }
                }
            }
        }
    }

    func start() async {
        do {
            guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: .current) else {
                guard active else { return }
                emit("fallback")
                dispose()
                return
            }
            guard active else { return }
            let transcriber = SpeechTranscriber(locale: locale, preset: .progressiveTranscription)
            if await AssetInventory.status(forModules: [transcriber]) != .installed {
                guard active else { return }
                emit("state", state: "preparing")
                if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                    guard active else { return }
                    try await request.downloadAndInstall()
                }
            }
            guard active else { return }
            let analyzer = SpeechAnalyzer(modules: [transcriber])
            self.analyzer = analyzer
            let engine = AVAudioEngine()
            let sourceFormat = engine.inputNode.outputFormat(forBus: 0)
            guard sourceFormat.sampleRate > 0, sourceFormat.channelCount > 0,
                  let format = await SpeechAnalyzer.bestAvailableAudioFormat(
                    compatibleWith: [transcriber], considering: sourceFormat),
                  let converter = AVAudioConverter(from: sourceFormat, to: format) else {
                throw captureError("No compatible microphone format is available.")
            }
            guard active else { return }
            try await analyzer.prepareToAnalyze(in: format)
            guard active else { return }
            let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream(bufferingPolicy: .bufferingOldest(32))
            input = continuation
            resultsTask = Task {
                do {
                    for try await result in transcriber.results {
                        guard active else { return }
                        let text = String(result.text.characters)
                        if result.isFinal {
                            finalized += text
                            volatile = ""
                        } else {
                            volatile = text
                        }
                        // A finalized segment is not the final result of the session.
                        emit("result", text: finalized + volatile)
                    }
                } catch {
                    fail(error)
                }
            }
            try await analyzer.start(inputSequence: stream)
            guard active, !finishing else { return }
            self.engine = engine
            engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: sourceFormat) { [weak self] buffer, _ in
                // The tap's buffer is borrowed. Conversion allocates an owned buffer
                // in the analyzer's format before yielding it across the async boundary.
                let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * format.sampleRate / sourceFormat.sampleRate)) + 1
                guard let converted = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return }
                var supplied = false
                var error: NSError?
                let status = converter.convert(to: converted, error: &error) { _, status in
                    if supplied {
                        status.pointee = .noDataNow
                        return nil
                    }
                    supplied = true
                    status.pointee = .haveData
                    return buffer
                }
                if status == .error {
                    let message = error?.localizedDescription ?? "Microphone conversion failed."
                    Task { @MainActor in
                        guard let self else { return }
                        self.fail(self.captureError(message))
                    }
                } else if converted.frameLength > 0 {
                    if case .dropped = continuation.yield(AnalyzerInput(buffer: converted)) {
                        Task { @MainActor in
                            guard let self else { return }
                            self.fail(self.captureError("Speech analysis could not keep up with the microphone."))
                        }
                    }
                }
            }
            engine.prepare()
            try engine.start()
            emit("state", state: "recording_analyzer")
        } catch {
            fail(error)
        }
    }

    func stop(finish: Bool) {
        guard active else { return }
        if !finish {
            dispose()
            return
        }
        guard !finishing else { return }
        finishing = true
        stopCapture()
        guard let analyzer, resultsTask != nil else {
            emit("result", text: finalized + volatile, final: true)
            emit("state", state: "stopped")
            dispose()
            return
        }
        Task {
            do {
                try await analyzer.finalizeAndFinishThroughEndOfInput()
                await resultsTask?.value
                guard active else { return }
                emit("result", text: finalized + volatile, final: true)
                emit("state", state: "stopped")
                dispose()
            } catch {
                fail(error)
            }
        }
    }

    func stopCapture() {
        if let engine {
            engine.stop()
            engine.inputNode.removeTap(onBus: 0)
            self.engine = nil
        }
        input?.finish()
        input = nil
    }

    func dispose() {
        active = false
        stopCapture()
        setupTask?.cancel()
        resultsTask?.cancel()
        if let analyzer { Task { await analyzer.cancelAndFinishNow() } }
        analyzer = nil
        if Self.current === self { Self.current = nil }
    }

    func fail(_ error: Error) {
        guard active else { return }
        emit("state", state: "error", error: error.localizedDescription)
        dispose()
    }

    func captureError(_ message: String) -> NSError {
        NSError(domain: "RCPDictation", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
