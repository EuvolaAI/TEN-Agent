/**
 *
 * Agora Real Time Engagement
 * Created by Hai Guo in 2024-08.
 * Copyright (c) 2024 Agora IO. All rights reserved.
 *
 */
// An extension written by Go for TTS
package extension

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/ioutil"
	"net/http"
	"ten_framework/ten"
	"time"

	"github.com/google/uuid"
)

type volcengineTTS struct {
	client *http.Client //?
	config volcengineTTSConfig
}

type volcengineTTSConfig struct {
	AppID                 string
	Token                 string
	Cluster               string
	Timbre                string
	SampleRate            int32
	SpeedRatio            float32
	VolumnRatio           float32
	PitchRatio            float32
	RequestTimeoutSeconds int
}

func defaultVolcengineTTSConfig() volcengineTTSConfig {
	return volcengineTTSConfig{
		AppID:                 "",
		Token:                 "",
		Cluster:               "",
		Timbre:                "",
		SampleRate:            24000,
		SpeedRatio:            1.0,
		VolumnRatio:           1.0,
		PitchRatio:            1.0,
		RequestTimeoutSeconds: 30,
	}
}

func newVolcengineTTS(config volcengineTTSConfig) (*volcengineTTS, error) {
	return &volcengineTTS{
		config: config,
		client: &http.Client{
			Transport: &http.Transport{
				MaxIdleConnsPerHost: 10,
				// Keep-Alive connection never expires
				IdleConnTimeout: time.Second * 0,
			},
			Timeout: time.Second * time.Duration(config.RequestTimeoutSeconds),
		},
	}, nil
}

// TTSServResponse response from backend srvs
type TTSServResponse struct {
	ReqID     string `json:"reqid"`
	Code      int    `json:"code"`
	Message   string `json:"Message"`
	Operation string `json:"operation"`
	Sequence  int    `json:"sequence"`
	Data      string `json:"data"`
}

func httpPost(url string, headers map[string]string, body []byte,
	timeout time.Duration) ([]byte, error) {
	client := &http.Client{
		Timeout: timeout,
	}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewBuffer(body))
	if err != nil {
		return nil, err
	}
	for key, value := range headers {
		req.Header.Set(key, value)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	retBody, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	return retBody, err
}

func (v *volcengineTTS) textToSpeechStream(tenEnv ten.TenEnv, streamWriter io.Writer, text string) (err error) {
	reqID := uuid.NewString()
	params := make(map[string]map[string]interface{})
	params["app"] = make(map[string]interface{})
	//填写平台申请的appid
	params["app"]["appid"] = v.config.AppID
	//这部分的token不生效，填写下方的默认值就好
	params["app"]["token"] = v.config.Token
	//填写平台上显示的集群名称
	params["app"]["cluster"] = v.config.Cluster
	params["user"] = make(map[string]interface{})
	//这部分如有需要，可以传递用户真实的ID，方便问题定位
	params["user"]["uid"] = "uid"
	params["audio"] = make(map[string]interface{})
	//填写选中的音色代号
	params["audio"]["voice_type"] = v.config.Timbre
	params["audio"]["rate"] = v.config.SampleRate
	params["audio"]["encoding"] = "pcm"
	params["audio"]["speed_ratio"] = v.config.SpeedRatio
	params["audio"]["volume_ratio"] = v.config.VolumnRatio
	params["audio"]["pitch_ratio"] = v.config.PitchRatio
	params["request"] = make(map[string]interface{})
	params["request"]["reqid"] = reqID
	params["request"]["text"] = text
	params["request"]["text_type"] = "plain"
	params["request"]["operation"] = "query"
	// 添加额外参数，禁用markdown过滤
	extraParam := map[string]interface{}{
		"disable_markdown_filter": true,
	}
	extraParamJSON, _ := json.Marshal(extraParam)
	params["request"]["extra_param"] = string(extraParamJSON)
	headers := make(map[string]string)
	headers["Content-Type"] = "application/json"
	//bearerToken为saas平台对应的接入认证中的Token
	headers["Authorization"] = fmt.Sprintf("Bearer;%s", v.config.Token)

	// URL查看上方第四点: 4.并发合成接口(POST)
	url := "https://openspeech.bytedance.com/api/v1/tts"
	bodyStr, _ := json.Marshal(params)
	synResp, err := httpPost(url, headers,
		[]byte(bodyStr),
		time.Second*time.Duration(v.config.RequestTimeoutSeconds),
	)
	if err != nil {
		tenEnv.LogError(fmt.Sprintf("http post fail [err:%s]\n", err.Error()))
		return err
	}
	var respJSON TTSServResponse
	err = json.Unmarshal(synResp, &respJSON)
	if err != nil {
		tenEnv.LogError(fmt.Sprintf("unmarshal response fail [err:%s]\n", err.Error()))
		return err
	}
	code := respJSON.Code
	if code != 3000 {
		tenEnv.LogError(fmt.Sprintf("code fail [code:%d]\n", code))
		return errors.New("resp code fail")
	}

	audio, _ := base64.StdEncoding.DecodeString(respJSON.Data)
	_, writeErr := streamWriter.Write(audio)
	if writeErr != nil {
		tenEnv.LogError(fmt.Sprintf("Failed to write to streamWriter, error: %s", writeErr))
		return fmt.Errorf("failed to write to streamWriter: %w", writeErr)
	}
	return nil
}
